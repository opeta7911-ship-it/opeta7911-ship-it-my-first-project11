import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from database.db import Database
from database.models import BumpHistory, Cycle, Filter, Lot, Setting
from playerok.client import LOT_LIFETIME_DAYS, MyLot, PlayerokClient

logger = logging.getLogger(__name__)

BumpCallback = Callable[[Lot, bool, int, str | None], Awaitable[None]]


class _BoardTracker:
    """Per-category board refresh timing tracker (one per top_position filter keyword)."""
    __slots__ = ("last_ts", "avg_interval", "refresh_detected")

    def __init__(self) -> None:
        self.last_ts: float | None = None
        self.avg_interval: float = 62.0
        self.refresh_detected: asyncio.Event = asyncio.Event()


class BumpEngine:
    """Движок поднятий: тикает раз в минуту, выбирает следующий лот и поднимает."""

    def __init__(
        self,
        db: Database,
        playerok: PlayerokClient,
        on_result: BumpCallback,
    ) -> None:
        self.db = db
        self.playerok = playerok
        self.on_result = on_result
        self._task: asyncio.Task | None = None
        self._live_lots_task: asyncio.Task | None = None
        self._smart_bump_task: asyncio.Task | None = None
        self._enabled = False
        self._live_lots: list[MyLot] = []
        self._startup_done: asyncio.Event = asyncio.Event()
        # Per-category board refresh trackers (keyword → tracker).
        # Each top_position filter keyword gets its own tracker so different
        # game boards (Brawl Stars, Roblox, …) calibrate independently.
        self._board_trackers: dict[str, _BoardTracker] = {}
        self._any_refresh: asyncio.Event = asyncio.Event()
        self._prev_expires: dict[str, datetime | None] = {}
        self._top_kw_poll_counter: int = 0
        # Set after a smart bump so _live_lots_loop pauses briefly to avoid
        # triggering Playerok rate limits from bump + immediate poll combination.
        self._smart_bumped_at: float = 0.0
        # Used to compute "time since engine start" for never-bumped filters
        # so they wait a full interval before their first bump (not immediately).
        self._engine_start_ts: datetime = datetime.utcnow()

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._enabled = True
        self._startup_done.clear()
        self._board_trackers.clear()
        self._any_refresh.clear()
        self._prev_expires.clear()
        self._top_kw_poll_counter = 0
        self._smart_bumped_at = 0.0
        self._engine_start_ts = datetime.utcnow()
        asyncio.create_task(self._startup_sync())
        self._live_lots_task = asyncio.create_task(self._live_lots_loop())
        self._smart_bump_task = asyncio.create_task(self._smart_bump_loop())
        self._task = asyncio.create_task(self._run_loop())

    async def _load_tracker(self, kw: str, tracker: _BoardTracker) -> None:
        """Restore saved calibration data from DB into a fresh tracker."""
        try:
            async with self.db.session_factory() as session:
                ts_row = await session.get(Setting, f"bt_ts_{kw}")
            if ts_row:
                saved_ts = float(ts_row.value)
                # Only restore last_ts if less than 5 minutes old —
                # older data can't be used for cycle prediction.
                age = datetime.utcnow().timestamp() - saved_ts
                # Only restore last_ts if < 5 min old (< 5 board cycles).
                # With avg_interval error of ~1s/cycle, 5 cycles = max ±5s phase error.
                # Beyond 5 min the accumulated drift exceeds the bump margin — better
                # to do a quick calibration bump than predict 10+ cycles ahead.
                if age < 300:
                    tracker.last_ts = saved_ts
                    logger.info(
                        "BoardTracker '%s': restored last_ts=%.0fs ago avg=%.0fs",
                        kw, age, tracker.avg_interval,
                    )
                else:
                    logger.info(
                        "BoardTracker '%s': avg=%.0fs restored, last_ts stale (%.0f min) — will calibrate",
                        kw, tracker.avg_interval, age / 60,
                    )
        except Exception as e:
            logger.debug("Failed to load tracker for '%s': %s", kw, e)

    def _save_tracker(self, kw: str, tracker: _BoardTracker) -> None:
        """Persist tracker calibration to DB (fire-and-forget task)."""
        asyncio.create_task(self._save_tracker_async(kw, tracker))

    async def _save_tracker_async(self, kw: str, tracker: _BoardTracker) -> None:
        try:
            async with self.db.session_factory() as session:
                for key, value in [
                    (f"bt_ts_{kw}", str(tracker.last_ts) if tracker.last_ts else None),
                ]:
                    if value is None:
                        continue
                    row = await session.get(Setting, key)
                    if row:
                        row.value = value
                    else:
                        session.add(Setting(key=key, value=value))
                await session.commit()
        except Exception as e:
            logger.debug("Failed to save tracker for '%s': %s", kw, e)

    async def _refresh_top_kw_cache(self) -> None:
        """Sync _board_trackers with enabled top_position filters in DB.
        New keywords get a fresh tracker loaded with saved calibration;
        removed keywords lose theirs."""
        try:
            async with self.db.session_factory() as session:
                rows = await session.execute(
                    select(Filter.keyword).where(
                        Filter.enabled.is_(True),
                        Filter.top_position.isnot(None),
                        Filter.keyword.isnot(None),
                    )
                )
                current_kws = {r[0].lower() for r in rows if r[0]}
            for kw in current_kws - self._board_trackers.keys():
                tracker = _BoardTracker()
                await self._load_tracker(kw, tracker)
                self._board_trackers[kw] = tracker
                logger.info("BoardTracker: added category '%s'", kw)
            for kw in list(self._board_trackers.keys() - current_kws):
                del self._board_trackers[kw]
                logger.info("BoardTracker: removed category '%s'", kw)
            logger.debug("top_position trackers: %s", list(self._board_trackers.keys()))
        except Exception as e:
            logger.warning("Failed to refresh top_position keyword cache: %s", e)

    async def _live_lots_loop(self) -> None:
        """Polls get_my_lots() every 25s: refreshes live lots AND detects board approvals
        via expires_at changes (expires_at = approval_date + LOT_LIFETIME_DAYS)."""
        await self._startup_done.wait()
        await self._refresh_top_kw_cache()
        if self._live_lots:
            self._prev_expires = {l.playerok_id: l.expires_at for l in self._live_lots}
            # Bootstrap trackers from current approval_date of already-fetched lots.
            # approval_date = expires_at - LOT_LIFETIME_DAYS = last board refresh time.
            # This eliminates the blind 60s fallback wait even after a cold restart:
            # we already KNOW when the board last processed each lot.
            now_ts = datetime.utcnow().timestamp()
            for lot in self._live_lots:
                if lot.expires_at is None:
                    continue
                bootstrap_ts = (lot.expires_at - timedelta(days=LOT_LIFETIME_DAYS)).timestamp()
                # Only use if within 5 min (≤5 cycles) — beyond that, accumulated
                # phase drift (N cycles × ~1s/cycle) exceeds the 1.5s bump margin.
                if now_ts - bootstrap_ts > 300:
                    continue
                lot_name_lower = lot.name.lower()
                for kw, tracker in self._board_trackers.items():
                    if kw in lot_name_lower and tracker.last_ts is None:
                        tracker.last_ts = bootstrap_ts
                        logger.info(
                            "BoardTracker '%s': bootstrapped from approval_date, last_ts=%.0fs ago avg=%.0fs",
                            kw, now_ts - bootstrap_ts, tracker.avg_interval,
                        )
            await asyncio.sleep(8)
        while self._enabled:
            try:
                # If a smart bump just fired, wait 20s before polling to avoid
                # triggering Playerok's rate limit (bump + tick + poll = too many).
                since_bump = datetime.utcnow().timestamp() - self._smart_bumped_at
                if since_bump < 20.0:
                    await asyncio.sleep(20.0 - since_bump)

                # Refresh top_position keyword cache every 10 polls (~2 min)
                self._top_kw_poll_counter += 1
                if self._top_kw_poll_counter % 10 == 0:
                    await self._refresh_top_kw_cache()

                lots = await self.playerok.get_my_lots()
                new_expires = {l.playerok_id: l.expires_at for l in lots}
                now_ts = datetime.utcnow().timestamp()

                # Build a name lookup for the current poll result
                pid_to_name = {l.playerok_id: l.name for l in lots}

                # Detect board approval: when expires_at changes for any lot, Playerok
                # updated its approval_date (= our bump was processed by the board cycle).
                # approval_ts = expires_at - LOT_LIFETIME_DAYS  ≈  board refresh time.
                # Route each approval to the matching per-category tracker.
                for pid, new_exp in new_expires.items():
                    old_exp = self._prev_expires.get(pid)
                    if old_exp is None or new_exp is None or new_exp == old_exp:
                        continue
                    approval_ts = (new_exp - timedelta(days=LOT_LIFETIME_DAYS)).timestamp()
                    # Ignore stale approvals (older than 3 min)
                    if now_ts - approval_ts > 180:
                        continue

                    lot_name = pid_to_name.get(pid, "")
                    lot_name_lower = lot_name.lower()

                    if not self._board_trackers:
                        logger.debug("Board approval ignored (no trackers active): '%s'", lot_name)
                        continue

                    matched_kw = next(
                        (kw for kw in self._board_trackers if kw in lot_name_lower), None
                    )
                    if matched_kw is None:
                        logger.debug("Board approval ignored (no matching tracker): '%s'", lot_name)
                        continue

                    tracker = self._board_trackers[matched_kw]
                    logger.info(
                        "Board approval [%s]: lot='%s' approved=%.0fs ago",
                        matched_kw, lot_name, now_ts - approval_ts,
                    )
                    if tracker.last_ts is not None:
                        interval = approval_ts - tracker.last_ts
                        logger.info(
                            "Board [%s] bump interval: %.0fs (fixed target=%.0fs)",
                            matched_kw, interval, tracker.avg_interval,
                        )
                    if tracker.last_ts is None or approval_ts > tracker.last_ts:
                        tracker.last_ts = approval_ts
                        tracker.refresh_detected.set()
                        self._any_refresh.set()
                        self._save_tracker(matched_kw, tracker)

                tracker_summary = " | ".join(
                    f"{kw}: last={datetime.utcfromtimestamp(t.last_ts).strftime('%H:%M:%S') if t.last_ts else 'none'} avg={t.avg_interval:.0f}s"
                    for kw, t in self._board_trackers.items()
                )
                logger.info(
                    "Live lots poll: %d lots | [%s]",
                    len(lots), tracker_summary or "no top_position trackers",
                )

                self._live_lots = lots
                self._prev_expires = new_expires
                # Flat 16s poll interval. Adaptive shorter intervals (4-8s) caused
                # rate limits when combined with simultaneous bump + minute tick.
                # 16s is fast enough for EMA calibration (detection within ~16s
                # of actual refresh) while keeping total API calls below Playerok limits.
                await asyncio.sleep(16.0)
            except Exception as e:
                logger.warning("Live lots refresh failed: %s — retry in 60s", e)
                await asyncio.sleep(60)

    async def _estimate_smart_bump_count(self) -> int:
        """Count how many lots will be bumped in the next smart cycle.
        Used to compute pre-refresh margin so the LAST lot lands ~0.5s before refresh."""
        async with self.db.session_factory() as session:
            rows = await session.execute(
                select(Filter).where(Filter.enabled.is_(True), Filter.top_position.isnot(None))
            )
            filters = rows.scalars().all()
        total = 0
        for flt in filters:
            if not flt.keyword or not self._filter_has_budget(flt):
                continue
            kw = flt.keyword.lower()
            n = flt.lots_per_trigger or 1
            matches = sum(1 for l in self._live_lots if kw in l.name.lower())
            total += min(n, matches)
        return max(1, total)

    def _compute_earliest_bump_target(self, margin: float) -> float | None:
        """Return the earliest next-bump timestamp across all per-category trackers."""
        now_ts = datetime.utcnow().timestamp()
        earliest: float | None = None
        for tracker in self._board_trackers.values():
            if tracker.last_ts is None:
                continue
            elapsed = now_ts - tracker.last_ts
            cycles_ahead = max(1, int(elapsed / tracker.avg_interval) + 1)
            target = tracker.last_ts + cycles_ahead * tracker.avg_interval - margin
            if earliest is None or target < earliest:
                earliest = target
        return earliest

    async def _smart_bump_loop(self) -> None:
        """Bump just before the predicted board refresh for each category.
        Each top_position filter keyword has its own _BoardTracker that
        self-calibrates independently via expires_at detection.
        Falls back to fixed interval when no detection data exists yet."""
        await self._startup_done.wait()
        await asyncio.sleep(3)

        while self._enabled:
            # Sync trackers in case filters were enabled/disabled since last poll refresh.
            await self._refresh_top_kw_cache()

            # Margin: (N-1) × 1.5s API overhead + 1.5s buffer so the LAST lot
            # lands ~1.5s before the refresh. Smaller margin = fewer competitors
            # can squeeze in after us. Risk: if refresh comes >1.5s early we miss
            # one cycle, but the EMA keeps variance well within ±2s after calibration.
            n_lots = await self._estimate_smart_bump_count()
            margin = max(1.5, (n_lots - 1) * 1.5 + 1.5)

            bump_target_ts = self._compute_earliest_bump_target(margin)

            if bump_target_ts is not None:
                now_ts = datetime.utcnow().timestamp()
                sleep_for = bump_target_ts - now_ts

                logger.info(
                    "SMART BUMP: target in %.1fs (trackers=%d, lots=%d, margin=%.1fs)",
                    sleep_for + margin, len(self._board_trackers), n_lots, margin,
                )

                while sleep_for > 0.5 and self._enabled:
                    # Sleep at most 2s, or wake immediately when any board refreshes.
                    self._any_refresh.clear()
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(self._any_refresh.wait()),
                            timeout=min(sleep_for, 2.0),
                        )
                    except asyncio.TimeoutError:
                        pass

                    new_target = self._compute_earliest_bump_target(margin)
                    if new_target is not None and new_target < bump_target_ts:
                        bump_target_ts = new_target
                        now_ts = datetime.utcnow().timestamp()
                        logger.info(
                            "SMART BUMP: refresh detected, retargeted to +%.1fs",
                            bump_target_ts - now_ts,
                        )

                    sleep_for = bump_target_ts - datetime.utcnow().timestamp()
            else:
                if not self._board_trackers:
                    # No top_position filters enabled — nothing to calibrate, just idle
                    await asyncio.sleep(10.0)
                    continue
                # No calibration data yet. expires_at only changes when WE bump, so
                # waiting for a detection without bumping is a deadlock. Do one
                # calibration bump now, then wait for the detection loop to record
                # the resulting expires_at change and set last_ts.
                logger.info("SMART BUMP: no calibration data — doing calibration bump to bootstrap timing")
                try:
                    await self._do_smart_bumps()
                    self._smart_bumped_at = datetime.utcnow().timestamp()
                except Exception:
                    logger.exception("Smart bump (calibration) failed")
                # Wait up to 30s for the poll loop to detect the expires_at change
                waited = 0.0
                while waited < 30.0 and self._enabled:
                    await asyncio.sleep(2.0)
                    waited += 2.0
                    if any(t.last_ts is not None for t in self._board_trackers.values()):
                        logger.info("SMART BUMP: calibrated after %.0fs — switching to smart timing", waited)
                        break
                # Restart loop: next iteration computes target from real last_ts
                continue

            if not self._enabled:
                break

            try:
                await self._do_smart_bumps()
                self._smart_bumped_at = datetime.utcnow().timestamp()
            except Exception:
                logger.exception("Smart bump failed")

            cooldown = max(
                (t.avg_interval for t in self._board_trackers.values()),
                default=62.0,
            )
            await asyncio.sleep(max(5.0, cooldown * 0.25))

    async def _do_smart_bumps(self) -> None:
        """Bump every top_position keyword filter at end of cycle (last in queue → first in row).
        We always bump regardless of cached priority_position because position data
        from get_my_lots is server-cached and often stale by several minutes."""
        now_utc = datetime.utcnow()
        async with self.db.session_factory() as session:
            rows = await session.execute(
                select(Filter)
                .where(Filter.enabled.is_(True), Filter.top_position.isnot(None))
                .options(selectinload(Filter.lots))
            )
            filters = rows.scalars().all()

        for flt in filters:
            if not flt.keyword or not self._filter_has_budget(flt):
                continue
            kw = flt.keyword.lower()
            matches = [l for l in self._live_lots if kw in l.name.lower()]
            if not matches:
                continue
            n = flt.lots_per_trigger or 1
            sorted_matches = self._sort_live_by_db_age(matches, flt.lots)
            tracker = self._board_trackers.get(kw)
            if tracker and tracker.last_ts:
                tracker_info = f"avg={tracker.avg_interval:.0f}s/calibrated"
            else:
                tracker_info = "avg=62s/fallback"
            logger.info(
                "SMART BUMP '%s' → %d lot(s) (top-%d, %s)",
                flt.name, min(n, len(sorted_matches)), flt.top_position, tracker_info,
            )
            for live in sorted_matches[:n]:
                db_lot = await self._get_or_create_keyword_lot(flt.id, live)
                if db_lot:
                    await self._bump_lot(db_lot)

    async def _startup_sync(self) -> None:
        """On startup, sync stored lots and populate _live_lots cache."""
        try:
            active = await self.playerok.get_my_lots()
        except Exception as e:
            logger.warning("Startup sync: get_my_lots failed: %s", e)
            self._startup_done.set()  # signal loop to start refreshing immediately
            return
        # Populate live lots cache immediately from startup data
        self._live_lots = active
        self._prev_expires = {l.playerok_id: l.expires_at for l in active}
        self._startup_done.set()

        active_ids = {a.playerok_id for a in active}

        async with self.db.session_factory() as session:
            result = await session.execute(
                select(Lot)
                .join(Lot.filter)
                .where(Lot.paused.is_(False), Filter.keyword.is_(None))
            )
            all_lots = result.scalars().all()

        active_by_id = {a.playerok_id: a for a in active}
        untracked = [a for a in active if a.playerok_id not in {l.playerok_id for l in all_lots}]
        updated = 0

        for lot in all_lots:
            a = active_by_id.get(lot.playerok_id)
            if a and a.expires_at is not None and a.expires_at != lot.expires_at:
                try:
                    async with self.db.session_factory() as session:
                        db_lot = await session.get(Lot, lot.id)
                        if db_lot:
                            db_lot.expires_at = a.expires_at
                            await session.commit()
                except Exception as e:
                    logger.warning("Startup sync: failed to refresh expires_at for lot #%d: %s", lot.id, e)

        for lot in all_lots:
            if lot.playerok_id in active_ids:
                continue
            match = self._find_match(lot, untracked)
            if match:
                try:
                    async with self.db.session_factory() as session:
                        db_lot = await session.get(Lot, lot.id)
                        if db_lot:
                            db_lot.playerok_id = match.playerok_id
                            db_lot.url = match.url
                            db_lot.price_kopecks = match.price_kopecks
                            if match.expires_at is not None:
                                db_lot.expires_at = match.expires_at
                            await session.commit()
                    untracked = [a for a in untracked if a.playerok_id != match.playerok_id]
                    updated += 1
                    logger.info("Startup sync: lot #%d → new playerok_id=%s", lot.id, match.playerok_id)
                except Exception as e:
                    logger.warning("Startup sync: failed to update lot #%d: %s", lot.id, e)
            else:
                logger.info("Startup sync: lot #%d not in active lots (sold, not yet re-listed)", lot.id)
        if updated:
            logger.info("Startup sync complete: %d lot(s) updated", updated)

    async def stop(self) -> None:
        self._enabled = False
        for task in (self._task, self._live_lots_task, self._smart_bump_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._live_lots_task = None
        self._smart_bump_task = None

    async def _run_loop(self) -> None:
        await self._sleep_to_next_minute()
        while self._enabled:
            try:
                await self._tick()
            except Exception:
                logger.exception("BumpEngine tick failed")
            await self._sleep_to_next_minute()

    async def _sleep_to_next_minute(self) -> None:
        now = datetime.now()
        next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        # +0.3s buffer so the tick always fires well past the minute boundary,
        # preventing the same cycle position from being calculated twice in a row.
        await asyncio.sleep(max(0.0, (next_minute - now).total_seconds()) + 0.3)

    async def _tick(self) -> None:
        now = datetime.now()
        now_utc = datetime.utcnow()
        # top_position filters are handled by _smart_bump_loop; tick handles the rest
        lots = await self._pick_next_lots(now, now_utc, self._live_lots)
        if not lots:
            logger.debug("TICK %s — no lot selected", now.strftime("%H:%M"))
            return

        for lot in lots:
            await self._bump_lot(lot)

    async def _pick_next_lots(self, now: datetime, now_utc: datetime, live_lots: list[MyLot]) -> list[Lot]:
        result: list[Lot] = []

        # Load all data in one session, then close it before any writes
        async with self.db.session_factory() as session:
            cycles_q = await session.execute(
                select(Cycle)
                .where(Cycle.enabled.is_(True))
                .options(selectinload(Cycle.filters).selectinload(Filter.lots))
            )
            cycles = cycles_q.scalars().all()

            indep_q = await session.execute(
                select(Filter)
                .outerjoin(Filter.cycle)
                .where(
                    Filter.enabled.is_(True),
                    Filter.top_position.is_(None),  # top_position filters handled by smart loop
                    or_(
                        Filter.cycle_id.is_(None),
                        Cycle.enabled.is_(False),
                    ),
                )
                .options(selectinload(Filter.lots))
            )
            indep_filters = indep_q.scalars().all()

        for cycle in cycles:
            lot = await self._pick_from_cycle(cycle, now, live_lots)
            if lot is None:
                bumped_filter_ids = {l.filter_id for l in result}
                lot = await self._pick_cycle_fallback(cycle, now_utc, live_lots, bumped_filter_ids)
                if lot is not None:
                    logger.info(
                        "CYCLE '%s' fallback — lot #%d (empty slot covered)",
                        cycle.name, lot.id,
                    )
            if lot is not None:
                result.append(lot)

        global_pick = await self._pick_independent_global(indep_filters, now_utc, live_lots)
        result.extend(global_pick)

        return result

    async def _pick_independent_global(
        self, filters: list[Filter], now_utc: datetime, live_lots: list[MyLot]
    ) -> list[Lot]:
        filter_candidates = []

        for flt in filters:
            if not flt.interval_minutes:
                logger.debug("SKIP filter '%s' (id=%d): interval not set", flt.name, flt.id)
                continue
            if not self._filter_has_budget(flt):
                logger.debug("SKIP filter '%s' (id=%d): budget exhausted", flt.name, flt.id)
                continue

            if flt.keyword:
                kw = flt.keyword.lower()
                matches = [l for l in live_lots if kw in l.name.lower()]
                if not matches:
                    logger.debug("SKIP filter '%s' (id=%d): keyword '%s' no matches", flt.name, flt.id, flt.keyword)
                    continue
                # Determine filter cooldown from DB lots' last_bumped_at.
                # If never bumped: count from engine start so the filter waits
                # a full interval before first bump (no immediate bump on start).
                all_bumped = [l.last_bumped_at for l in flt.lots if l.last_bumped_at]
                filter_last_bump = max(all_bumped) if all_bumped else None
                if filter_last_bump is not None:
                    elapsed = (now_utc - filter_last_bump).total_seconds() / 60
                    # Overdue (missed interval) — don't flood-bump on startup,
                    # treat as if first bump and wait full interval from engine start.
                    if elapsed >= flt.interval_minutes:
                        elapsed = (now_utc - self._engine_start_ts).total_seconds() / 60
                else:
                    elapsed = (now_utc - self._engine_start_ts).total_seconds() / 60
                filter_on_cooldown = elapsed < max(0, flt.interval_minutes - 1)
                filter_candidates.append((filter_on_cooldown, filter_last_bump, flt, matches, True))
            else:
                lots = [l for l in flt.lots if not l.paused]
                if not lots:
                    logger.debug("SKIP filter '%s' (id=%d): no lots", flt.name, flt.id)
                    continue

                def _elapsed(l: Lot) -> float:
                    return (now_utc - l.last_bumped_at).total_seconds() / 60 if l.last_bumped_at else float("inf")

                def _lot_key(l: Lot):
                    on_cd = _elapsed(l) < max(0, flt.interval_minutes - 1)
                    return (
                        on_cd,
                        l.expires_at is None,
                        l.expires_at or datetime.max,
                        l.last_bumped_at is not None,
                        l.last_bumped_at or datetime.min,
                        l.id,
                    )

                lots.sort(key=_lot_key)
                all_bumped = [l.last_bumped_at for l in lots if l.last_bumped_at is not None]
                filter_last_bump = max(all_bumped) if all_bumped else None
                # Never-bumped lots: count from engine start so the first bump
                # only fires after a full interval, not immediately on activation.
                if lots[0].last_bumped_at is not None:
                    lot_elapsed = _elapsed(lots[0])
                    if lot_elapsed >= flt.interval_minutes:
                        # Overdue — wait full interval from engine start, not immediately
                        lot_elapsed = (now_utc - self._engine_start_ts).total_seconds() / 60
                    filter_on_cooldown = lot_elapsed < max(0, flt.interval_minutes - 1)
                else:
                    since_start = (now_utc - self._engine_start_ts).total_seconds() / 60
                    filter_on_cooldown = since_start < max(0, flt.interval_minutes - 1)
                filter_candidates.append((filter_on_cooldown, filter_last_bump, flt, lots, False))

        if not filter_candidates:
            logger.debug("ROBIN: no candidates this tick")
            return []

        filter_candidates.sort(
            key=lambda x: (x[0], x[1] is not None, x[1] or datetime.min, x[2].id)
        )

        logger.info(
            "ROBIN candidates: %s",
            ", ".join(
                f"'{c[2].name}'(cd={c[0]},last={c[1].strftime('%H:%M:%S') if c[1] else 'never'})"
                for c in filter_candidates
            ),
        )

        filter_on_cooldown, filter_last_bump, best_flt, data, is_keyword = filter_candidates[0]
        if filter_on_cooldown:
            logger.debug("ROBIN: best candidate '%s' is on cooldown, skipping", best_flt.name)
            return []
        n = best_flt.lots_per_trigger or 1
        logger.info("ROBIN selected: '%s' (keyword=%s) → %d lot(s)", best_flt.name, is_keyword, n)

        if is_keyword:
            # Sort by last_bumped_at from DB so just-bumped lots rotate to back
            sorted_matches = self._sort_live_by_db_age(data, best_flt.lots)
            result = []
            for live in sorted_matches[:n]:
                db_lot = await self._get_or_create_keyword_lot(best_flt.id, live)
                if db_lot:
                    result.append(db_lot)
            return result
        else:
            return data[:n]

    async def _pick_from_cycle(self, cycle: Cycle, now: datetime, live_lots: list[MyLot]) -> Lot | None:
        # Build list of (filter, slot_count) for schedule
        filter_slots: list[tuple[Filter, int]] = []
        for flt in sorted(cycle.filters, key=lambda f: f.order_index):
            if not flt.enabled or not self._filter_has_budget(flt):
                continue
            if flt.keyword:
                kw = flt.keyword.lower()
                matches = [l for l in live_lots if kw in l.name.lower()]
                n = len(matches) if matches else 0
            else:
                n = len([l for l in flt.lots if not l.paused])
            if n > 0:
                filter_slots.append((flt, n))

        if not filter_slots:
            return None

        schedule = self._build_cycle_schedule(filter_slots, cycle.duration_minutes)
        total_slots = sum(s for _, s in filter_slots)
        filled = len(schedule)
        logger.info(
            "CYCLE '%s': %d filters, %d lots, %d/%d min filled",
            cycle.name, len(filter_slots), total_slots, filled, cycle.duration_minutes,
        )

        try:
            sh, sm = (int(p) for p in cycle.start_time.split(":"))
        except ValueError:
            return None
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if start > now:
            start -= timedelta(days=1)
        elapsed_min = int((now - start).total_seconds() // 60)
        pos_in_cycle = elapsed_min % cycle.duration_minutes

        scheduled_flt = schedule.get(pos_in_cycle)
        if not scheduled_flt:
            logger.debug("CYCLE '%s' pos=%d — empty slot", cycle.name, pos_in_cycle)
            return None

        if scheduled_flt.keyword:
            kw = scheduled_flt.keyword.lower()
            matches = [l for l in live_lots if kw in l.name.lower()]
            if not matches:
                logger.info(
                    "CYCLE '%s' pos=%d — keyword '%s': no matches on Playerok",
                    cycle.name, pos_in_cycle, scheduled_flt.keyword,
                )
                return None
            best_live = self._sort_live_by_db_age(matches, scheduled_flt.lots)[0]
            logger.info(
                "CYCLE '%s' pos=%d — keyword '%s' → '%s' last=%s",
                cycle.name, pos_in_cycle, scheduled_flt.keyword, best_live.name,
                next((l.last_bumped_at.strftime("%H:%M") for l in scheduled_flt.lots
                      if l.playerok_id == best_live.playerok_id and l.last_bumped_at), "never"),
            )
            return await self._get_or_create_keyword_lot(scheduled_flt.id, best_live)
        else:
            filter_lots = [l for l in scheduled_flt.lots if not l.paused]
            if not filter_lots:
                return None
            lot = min(filter_lots, key=lambda l: (
                l.expires_at is None,
                l.expires_at or datetime.max,
                l.last_bumped_at is not None,
                l.last_bumped_at or datetime.min,
                l.id,
            ))
            logger.info(
                "CYCLE '%s' pos=%d — lot #%d last=%s expires=%s",
                cycle.name, pos_in_cycle, lot.id,
                lot.last_bumped_at.strftime("%H:%M") if lot.last_bumped_at else "never",
                lot.expires_at.strftime("%d.%m") if lot.expires_at else "?",
            )
            return lot

    async def _pick_cycle_fallback(
        self,
        cycle: Cycle,
        now_utc: datetime,
        live_lots: list[MyLot],
        exclude_filter_ids: set[int] | None = None,
    ) -> Lot | None:
        """Round-robin fallback for empty cycle slots — picks the filter bumped longest ago."""
        candidates = []
        for flt in cycle.filters:
            if not flt.enabled or not self._filter_has_budget(flt):
                continue
            if exclude_filter_ids and flt.id in exclude_filter_ids:
                continue

            if flt.keyword:
                kw = flt.keyword.lower()
                matches = [l for l in live_lots if kw in l.name.lower()]
                if not matches:
                    continue
                all_bumped = [l.last_bumped_at for l in flt.lots if l.last_bumped_at]
                filter_last_bump = max(all_bumped) if all_bumped else None
                candidates.append((filter_last_bump, flt, matches, True))
            else:
                lots = [l for l in flt.lots if not l.paused]
                if not lots:
                    continue
                all_bumped = [l.last_bumped_at for l in lots if l.last_bumped_at is not None]
                filter_last_bump = max(all_bumped) if all_bumped else None
                candidates.append((filter_last_bump, flt, lots, False))

        if not candidates:
            return None

        candidates.sort(key=lambda x: (x[0] is not None, x[0] or datetime.min))
        filter_last_bump, best_flt, data, is_keyword = candidates[0]

        if is_keyword:
            best_live = self._sort_live_by_db_age(data, best_flt.lots)[0]
            return await self._get_or_create_keyword_lot(best_flt.id, best_live)
        else:
            return min(data, key=lambda l: (
                l.expires_at is None,
                l.expires_at or datetime.max,
                l.last_bumped_at is not None,
                l.last_bumped_at or datetime.min,
                l.id,
            ))

    @staticmethod
    def _build_cycle_schedule(
        filter_slots: list[tuple[Filter, int]], duration: int
    ) -> dict[int, Filter]:
        """
        Assigns each filter a set of evenly-spaced minutes.
        Returns dict: minute_pos -> Filter.
        Filters with more lots fire more often and claim slots first.
        No two filters ever share the same minute.
        """
        sorted_items = sorted(filter_slots, key=lambda x: -x[1])
        schedule: dict[int, Filter] = {}

        for flt, n in sorted_items:
            n = min(n, duration)
            step = duration / n
            for offset in range(duration):
                positions = [round(offset + k * step) % duration for k in range(n)]
                if len(set(positions)) == n and all(p not in schedule for p in positions):
                    for p in positions:
                        schedule[p] = flt
                    break

        return schedule

    @staticmethod
    def _sort_live_by_db_age(live_lots: list[MyLot], db_lots: list[Lot]) -> list[MyLot]:
        """Sort live lots oldest-first using last_bumped_at from DB records as primary key.
        Falls back to expires_at when a lot has never been bumped (no DB record yet)."""
        db_bumped: dict[str, datetime] = {
            l.playerok_id: l.last_bumped_at
            for l in db_lots
            if l.playerok_id and l.last_bumped_at
        }
        return sorted(live_lots, key=lambda l: (
            db_bumped.get(l.playerok_id) or datetime.min,
            l.expires_at if l.expires_at is not None else datetime.min,
            l.playerok_id,
        ))

    async def _get_or_create_keyword_lot(self, filter_id: int, live: MyLot) -> Lot | None:
        """Find or create a DB Lot record for a keyword-matched live lot."""
        async with self.db.session_factory() as session:
            # Try by playerok_id (exact match — most common case after first bump)
            result = await session.execute(
                select(Lot).where(Lot.playerok_id == live.playerok_id)
            )
            db_lot = result.scalar_one_or_none()
            if db_lot:
                db_lot.filter_id = filter_id
                db_lot.expires_at = live.expires_at
                db_lot.price_kopecks = live.price_kopecks
                db_lot.name = live.name
                db_lot.paused = False
                await session.commit()
                return db_lot

            # Create new record
            try:
                db_lot = Lot(
                    filter_id=filter_id,
                    playerok_id=live.playerok_id,
                    url=live.url,
                    name=live.name,
                    price_kopecks=live.price_kopecks,
                    bump_cost_kopecks=0,
                    expires_at=live.expires_at,
                )
                session.add(db_lot)
                await session.commit()
                await session.refresh(db_lot)
                return db_lot
            except Exception as e:
                await session.rollback()
                logger.warning("_get_or_create_keyword_lot create failed: %s", e)
                # Unique URL constraint — find by URL
                try:
                    result2 = await session.execute(select(Lot).where(Lot.url == live.url))
                    existing = result2.scalar_one_or_none()
                    if existing:
                        existing.filter_id = filter_id
                        existing.playerok_id = live.playerok_id
                        existing.expires_at = live.expires_at
                        existing.price_kopecks = live.price_kopecks
                        existing.paused = False
                        await session.commit()
                        return existing
                except Exception:
                    pass
                return None

    @staticmethod
    def _filter_has_budget(flt: Filter) -> bool:
        if flt.spend_limit_kopecks is None:
            return True
        return flt.spent_kopecks < flt.spend_limit_kopecks

    _SOLD_PHRASES = ("нельзя обновить статус", "item not found", "не найден")

    def _price_rub_for_bump(self, playerok_id: str, fallback_kopecks: int) -> float:
        """Return the raw (pre-discount) price in rubles for get_item_priority_statuses.
        Playerok validates boosters against rawPrice; using the discounted price causes
        'некорректных бустеров' errors for discounted lots."""
        live = next((l for l in self._live_lots if l.playerok_id == playerok_id), None)
        if live and live.raw_price_kopecks > 0:
            return live.raw_price_kopecks / 100
        return fallback_kopecks / 100

    async def _bump_lot(self, lot: Lot) -> None:
        price_rub = self._price_rub_for_bump(lot.playerok_id, lot.price_kopecks)
        try:
            cost, status_id = await self.playerok.refresh_bump_cost(
                lot.playerok_id, price_rub
            )
        except Exception as exc:
            err = str(exc)
            if any(p in err.lower() for p in self._SOLD_PHRASES):
                refreshed = await self._refresh_lot_id(lot)
                if refreshed is None:
                    logger.info("Lot #%d sold, no re-listing found yet — skipping tick", lot.id)
                    async with self.db.session_factory() as session:
                        db_lot = await session.get(Lot, lot.id)
                        if db_lot:
                            db_lot.last_bumped_at = datetime.utcnow()
                            await session.commit()
                    return
                lot = refreshed
                price_rub = self._price_rub_for_bump(lot.playerok_id, lot.price_kopecks)
                try:
                    cost, status_id = await self.playerok.refresh_bump_cost(
                        lot.playerok_id, price_rub
                    )
                except Exception as exc2:
                    await self._record_failure(lot.id, str(exc2))
                    return
            else:
                await self._record_failure(lot.id, f"Не удалось получить цену поднятия: {exc}")
                return

        try:
            await self.playerok.bump(lot.playerok_id, status_id)
        except Exception as exc:
            err = str(exc)
            if any(p in err.lower() for p in self._SOLD_PHRASES):
                logger.info("Lot #%d sold during bump — searching for re-listing", lot.id)
                refreshed = await self._refresh_lot_id(lot)
                async with self.db.session_factory() as session:
                    db_lot = await session.get(Lot, lot.id)
                    if db_lot:
                        db_lot.last_bumped_at = datetime.utcnow()
                        await session.commit()
                if refreshed is None:
                    logger.info("Lot #%d: no re-listing found yet — skipping tick", lot.id)
                else:
                    logger.info("Lot #%d refreshed to playerok_id=%s — will bump next tick", lot.id, refreshed.playerok_id)
                return
            await self._record_failure(lot.id, err)
            return

        await self._record_success(lot.id, cost)

    @staticmethod
    def _normalize(s: str) -> str:
        return " ".join(s.strip().lower().split())

    @staticmethod
    def _find_match(lot: Lot, candidates: list[MyLot]) -> "MyLot | None":
        if not candidates:
            return None
        norm = BumpEngine._normalize(lot.name)
        by_name = [c for c in candidates if BumpEngine._normalize(c.name) == norm]
        pool = by_name if by_name else candidates
        within_price = [c for c in pool if abs(c.price_kopecks - lot.price_kopecks) <= lot.price_kopecks * 0.1]
        if within_price:
            return min(within_price, key=lambda c: abs(c.price_kopecks - lot.price_kopecks))
        if by_name:
            return min(by_name, key=lambda c: abs(c.price_kopecks - lot.price_kopecks))
        return None

    async def _refresh_lot_id(self, lot: Lot) -> Lot | None:
        """Find a re-listed version of a sold lot and update the DB."""
        try:
            active = await self.playerok.get_my_lots()
        except Exception as e:
            logger.warning("get_my_lots failed for lot #%d: %s", lot.id, e)
            return None

        async with self.db.session_factory() as session:
            rows = await session.execute(select(Lot.playerok_id))
            tracked = {r[0] for r in rows}

        untracked = [a for a in active if a.playerok_id not in tracked]
        logger.info(
            "Lot #%d refresh: name='%s' price=%d — %d untracked candidates from %d active",
            lot.id, lot.name, lot.price_kopecks, len(untracked), len(active),
        )

        match = self._find_match(lot, untracked)
        if not match:
            return None

        try:
            async with self.db.session_factory() as session:
                db_lot = await session.get(Lot, lot.id)
                if db_lot is None:
                    return None
                db_lot.playerok_id = match.playerok_id
                db_lot.url = match.url
                db_lot.price_kopecks = match.price_kopecks
                if match.expires_at is not None:
                    db_lot.expires_at = match.expires_at
                await session.commit()
        except Exception as e:
            logger.warning("Failed to update lot #%d in DB: %s", lot.id, e)
            return None

        logger.info("Lot #%d refreshed: %s → %s", lot.id, lot.playerok_id, match.playerok_id)
        lot.playerok_id = match.playerok_id
        lot.url = match.url
        lot.price_kopecks = match.price_kopecks
        if match.expires_at is not None:
            lot.expires_at = match.expires_at
        return lot

    async def _record_success(self, lot_id: int, cost_kopecks: int) -> None:
        async with self.db.session_factory() as session:
            lot = await session.get(Lot, lot_id, options=[selectinload(Lot.filter)])
            if lot is None:
                return
            flt = lot.filter
            now = datetime.utcnow()

            # 24h rolling limit reset: if 24h have passed since period start → reset
            if flt.spend_limit_kopecks is not None and flt.limit_reset_at is not None:
                if (now - flt.limit_reset_at).total_seconds() >= 86400:
                    flt.spent_kopecks = 0
                    flt.limit_reset_at = None

            # Record period start (on first spend, or if cleared by restart)
            if flt.spend_limit_kopecks is not None and flt.limit_reset_at is None:
                flt.limit_reset_at = now

            lot.last_bumped_at = now
            lot.bump_cost_kopecks = cost_kopecks
            flt.spent_kopecks += cost_kopecks
            session.add(BumpHistory(lot_id=lot.id, success=True, cost_kopecks=cost_kopecks))
            await session.commit()
            await session.refresh(lot, ["filter"])
            await self.on_result(lot, True, cost_kopecks, None)

    async def _record_failure(self, lot_id: int, error: str) -> None:
        async with self.db.session_factory() as session:
            lot = await session.get(Lot, lot_id, options=[selectinload(Lot.filter)])
            if lot is None:
                return
            lot.last_bumped_at = datetime.utcnow()
            session.add(BumpHistory(lot_id=lot.id, success=False, error=error))
            await session.commit()
            await self.on_result(lot, False, 0, error)
