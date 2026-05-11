import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from database.db import Database
from database.models import BumpHistory, Cycle, Filter, Lot
from playerok.client import LOT_LIFETIME_DAYS, MyLot, PlayerokClient

logger = logging.getLogger(__name__)

BumpCallback = Callable[[Lot, bool, int, str | None], Awaitable[None]]



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
        # Board refresh timing (for smart top-position bumping)
        self._last_board_refresh_ts: float | None = None
        self._avg_refresh_interval: float = 62.0
        self._prev_expires: dict[str, datetime | None] = {}
        # Fired immediately when a board refresh is detected — smart_bump_loop
        # reacts within ms instead of waiting up to 2s in the sleep chunk.
        self._refresh_detected: asyncio.Event = asyncio.Event()
        # Keywords from enabled top_position filters — only expires_at changes
        # on these lots are used for board-refresh timing. Lots in other categories
        # (Robux, etc.) have independent refresh cycles and must not corrupt timing.
        self._top_position_keywords: list[str] = []
        self._top_kw_poll_counter: int = 0
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
        self._last_board_refresh_ts = None
        self._avg_refresh_interval = 62.0
        self._prev_expires.clear()
        self._refresh_detected.clear()
        self._top_position_keywords = []
        self._top_kw_poll_counter = 0
        self._engine_start_ts = datetime.utcnow()
        asyncio.create_task(self._startup_sync())
        self._live_lots_task = asyncio.create_task(self._live_lots_loop())
        self._smart_bump_task = asyncio.create_task(self._smart_bump_loop())
        self._task = asyncio.create_task(self._run_loop())

    async def _refresh_top_kw_cache(self) -> None:
        """Reload the keyword list for top_position filters from DB."""
        try:
            async with self.db.session_factory() as session:
                rows = await session.execute(
                    select(Filter.keyword).where(
                        Filter.enabled.is_(True),
                        Filter.top_position.isnot(None),
                        Filter.keyword.isnot(None),
                    )
                )
                self._top_position_keywords = [r[0].lower() for r in rows if r[0]]
                logger.debug("top_position keywords: %s", self._top_position_keywords)
        except Exception as e:
            logger.warning("Failed to refresh top_position keyword cache: %s", e)

    async def _live_lots_loop(self) -> None:
        """Polls get_my_lots() every 25s: refreshes live lots AND detects board approvals
        via expires_at changes (expires_at = approval_date + LOT_LIFETIME_DAYS)."""
        await self._startup_done.wait()
        await self._refresh_top_kw_cache()
        if self._live_lots:
            self._prev_expires = {l.playerok_id: l.expires_at for l in self._live_lots}
            await asyncio.sleep(8)
        while self._enabled:
            try:
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
                # IMPORTANT: only track lots that match a top_position filter keyword.
                # Lots in other categories (Robux, etc.) refresh on an independent cycle
                # and must not corrupt our BRAWL PASS PLUS smart-bump timing.
                for pid, new_exp in new_expires.items():
                    old_exp = self._prev_expires.get(pid)
                    if old_exp is None or new_exp is None or new_exp == old_exp:
                        continue
                    approval_ts = (new_exp - timedelta(days=LOT_LIFETIME_DAYS)).timestamp()
                    # Ignore stale approvals (older than 3 min)
                    if now_ts - approval_ts > 180:
                        continue

                    # Skip lots not belonging to a top_position filter category.
                    # If cache is empty (no top_position filters enabled) — skip ALL lots:
                    # better to use fixed 62s fallback than pollute timing with other boards.
                    lot_name = pid_to_name.get(pid, "")
                    if not self._top_position_keywords or not any(
                        kw in lot_name.lower() for kw in self._top_position_keywords
                    ):
                        logger.debug(
                            "Board approval ignored for smart timing: '%s'",
                            lot_name,
                        )
                        continue

                    logger.info(
                        "Board approval via expires_at: lot=%s ('%s') approved=%.0fs ago",
                        pid, lot_name, now_ts - approval_ts,
                    )
                    if self._last_board_refresh_ts is not None:
                        interval = approval_ts - self._last_board_refresh_ts
                        if 15 < interval < 300:
                            # Detect missed detections (e.g. after rate-limit gap):
                            # if the gap is much longer than expected, treat it as
                            # multiple cycles and feed the per-cycle value to EMA.
                            n_cycles = max(1, round(interval / self._avg_refresh_interval))
                            effective = interval / n_cycles
                            # EMA (α=0.4): adapts within 3-4 samples
                            self._avg_refresh_interval = (
                                0.4 * effective + 0.6 * self._avg_refresh_interval
                            )
                            if n_cycles > 1:
                                logger.info(
                                    "Board refresh interval: %.0fs (%d cycles → %.1fs each) avg=%.0fs",
                                    interval, n_cycles, effective, self._avg_refresh_interval,
                                )
                            else:
                                logger.info(
                                    "Board refresh interval: %.0fs  avg=%.0fs",
                                    interval, self._avg_refresh_interval,
                                )
                    if self._last_board_refresh_ts is None or approval_ts > self._last_board_refresh_ts:
                        self._last_board_refresh_ts = approval_ts
                        self._refresh_detected.set()  # wake smart_bump_loop immediately

                logger.info(
                    "Live lots poll: %d lots | last_approval=%s avg_interval=%.0fs",
                    len(lots),
                    datetime.utcfromtimestamp(self._last_board_refresh_ts).strftime("%H:%M:%S")
                        if self._last_board_refresh_ts else "none",
                    self._avg_refresh_interval,
                )

                self._live_lots = lots
                self._prev_expires = new_expires
                # Adaptive poll: 4s when within 12s of predicted refresh,
                # 12s otherwise. 4s is fast enough to catch early refreshes
                # while staying well under Playerok's rate limit.
                poll_sleep = 12.0
                if self._last_board_refresh_ts is not None:
                    now_ts = datetime.utcnow().timestamp()
                    elapsed = now_ts - self._last_board_refresh_ts
                    time_in_cycle = elapsed % self._avg_refresh_interval
                    time_to_next = self._avg_refresh_interval - time_in_cycle
                    if time_to_next < 12:
                        poll_sleep = 4.0
                await asyncio.sleep(poll_sleep)
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

    async def _smart_bump_loop(self) -> None:
        """Bump just before the predicted board refresh.
        Margin adapts to lots_per_trigger: N lots × 1.5s/lot so the LAST lot
        lands ~2s before the refresh — minimising the window for competitors.
        Self-calibrates via expires_at detection; falls back to fixed interval."""
        await self._startup_done.wait()
        await asyncio.sleep(3)

        while self._enabled:
            # Margin: last lot bumped ~3s before refresh.
            # Refresh-interval variance observed in logs reaches ±3s
            # (59s..65s while avg=62s). 3s buffer ensures we never bump
            # AFTER a refresh even when it comes early.
            # Formula: (N-1) × 1.5s API overhead + 3s buffer.
            n_lots = await self._estimate_smart_bump_count()
            margin = max(3.0, (n_lots - 1) * 1.5 + 3.0)

            if self._last_board_refresh_ts is not None:
                now_ts = datetime.utcnow().timestamp()
                interval = self._avg_refresh_interval
                elapsed = now_ts - self._last_board_refresh_ts
                cycles_ahead = max(1, int(elapsed / interval) + 1)
                bump_target_ts = self._last_board_refresh_ts + cycles_ahead * interval - margin
                baseline_refresh_ts = self._last_board_refresh_ts

                sleep_for = bump_target_ts - now_ts
                logger.info(
                    "SMART BUMP: next refresh in %.1fs (last=%.0fs ago, avg=%.0fs, lots=%d, margin=%.1fs)",
                    sleep_for + margin, elapsed, interval, n_lots, margin,
                )

                while sleep_for > 0.5 and self._enabled:
                    # Wait at most 2s OR wake immediately when refresh detected.
                    self._refresh_detected.clear()
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(self._refresh_detected.wait()),
                            timeout=min(sleep_for, 2.0),
                        )
                    except asyncio.TimeoutError:
                        pass
                    now_ts = datetime.utcnow().timestamp()

                    if self._last_board_refresh_ts is not None:
                        new_iv = self._avg_refresh_interval
                        new_elapsed = now_ts - self._last_board_refresh_ts
                        new_cycles = max(1, int(new_elapsed / new_iv) + 1)
                        new_target = self._last_board_refresh_ts + new_cycles * new_iv - margin

                        if new_target < bump_target_ts:
                            # Refresh came earlier than predicted — pull target forward.
                            bump_target_ts = new_target
                            logger.info(
                                "SMART BUMP: early refresh detected, retargeted to +%.1fs",
                                new_target - now_ts,
                            )
                        elif (self._last_board_refresh_ts > baseline_refresh_ts
                              and bump_target_ts > self._last_board_refresh_ts
                              and new_target > now_ts + 1):
                            bump_target_ts = new_target
                            baseline_refresh_ts = self._last_board_refresh_ts
                            logger.info(
                                "SMART BUMP: board refreshed during sleep, retargeted to +%.1fs",
                                new_target - now_ts,
                            )

                    sleep_for = bump_target_ts - now_ts
            else:
                # No board refresh data yet — wait the full interval BEFORE bumping
                # to prevent rapid-fire bumps every 15s on fresh start.
                interval_wait = self._avg_refresh_interval
                logger.info(
                    "SMART BUMP: no refresh detected — waiting %.0fs before bump",
                    interval_wait,
                )
                detection_fired = False
                waited = 0.0
                while waited < interval_wait and self._enabled:
                    await asyncio.sleep(2.0)
                    waited += 2.0
                    if self._last_board_refresh_ts is not None:
                        # Detection fired — skip this bump, next iteration uses smart timing
                        logger.info("SMART BUMP: detection fired during wait — switching to smart timing")
                        detection_fired = True
                        break
                if detection_fired:
                    continue  # restart loop with smart timing

            if not self._enabled:
                break

            try:
                await self._do_smart_bumps()
            except Exception:
                logger.exception("Smart bump failed")

            await asyncio.sleep(max(5.0, self._avg_refresh_interval * 0.25))

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
            logger.info(
                "SMART BUMP '%s' → %d lot(s) (top-%d, avg=%.0fs, detection=%s)",
                flt.name, min(n, len(sorted_matches)), flt.top_position,
                self._avg_refresh_interval,
                "ON/expires_at" if self._last_board_refresh_ts else "OFF/fixed",
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
