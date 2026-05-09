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
        self._refresh_intervals: list[float] = []
        self._prev_expires: dict[str, datetime | None] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._enabled = True
        self._startup_done.clear()
        self._last_board_refresh_ts = None
        self._refresh_intervals.clear()
        self._prev_expires.clear()
        asyncio.create_task(self._startup_sync())
        self._live_lots_task = asyncio.create_task(self._live_lots_loop())
        self._smart_bump_task = asyncio.create_task(self._smart_bump_loop())
        self._task = asyncio.create_task(self._run_loop())

    async def _live_lots_loop(self) -> None:
        """Polls get_my_lots() every 25s: refreshes live lots AND detects board approvals
        via expires_at changes (expires_at = approval_date + LOT_LIFETIME_DAYS)."""
        await self._startup_done.wait()
        if self._live_lots:
            self._prev_expires = {l.playerok_id: l.expires_at for l in self._live_lots}
            await asyncio.sleep(8)
        while self._enabled:
            try:
                lots = await self.playerok.get_my_lots()
                new_expires = {l.playerok_id: l.expires_at for l in lots}
                now_ts = datetime.utcnow().timestamp()

                # Detect board approval: when expires_at changes for any lot, Playerok
                # updated its approval_date (= our bump was processed by the board cycle).
                # approval_ts = expires_at - LOT_LIFETIME_DAYS  ≈  board refresh time.
                for pid, new_exp in new_expires.items():
                    old_exp = self._prev_expires.get(pid)
                    if old_exp is None or new_exp is None or new_exp == old_exp:
                        continue
                    approval_ts = (new_exp - timedelta(days=LOT_LIFETIME_DAYS)).timestamp()
                    # Ignore stale approvals (older than 3 min)
                    if now_ts - approval_ts > 180:
                        continue
                    logger.info(
                        "Board approval via expires_at: lot=%s approved=%.0fs ago",
                        pid, now_ts - approval_ts,
                    )
                    if self._last_board_refresh_ts is not None:
                        interval = approval_ts - self._last_board_refresh_ts
                        if 15 < interval < 300:
                            self._refresh_intervals.append(interval)
                            if len(self._refresh_intervals) > 5:
                                self._refresh_intervals.pop(0)
                            self._avg_refresh_interval = (
                                sum(self._refresh_intervals) / len(self._refresh_intervals)
                            )
                            logger.info(
                                "Board refresh interval: %.0fs  avg=%.0fs",
                                interval, self._avg_refresh_interval,
                            )
                    if self._last_board_refresh_ts is None or approval_ts > self._last_board_refresh_ts:
                        self._last_board_refresh_ts = approval_ts

                logger.info(
                    "Live lots poll: %d lots | last_approval=%s avg_interval=%.0fs",
                    len(lots),
                    datetime.utcfromtimestamp(self._last_board_refresh_ts).strftime("%H:%M:%S")
                        if self._last_board_refresh_ts else "none",
                    self._avg_refresh_interval,
                )

                self._live_lots = lots
                self._prev_expires = new_expires
                await asyncio.sleep(8)
            except Exception as e:
                logger.warning("Live lots refresh failed: %s — retry in 60s", e)
                await asyncio.sleep(60)

    async def _smart_bump_loop(self) -> None:
        """Bump 1.5s before the predicted board refresh.
        Self-calibrates via expires_at detection; falls back to fixed interval until first detection."""
        await self._startup_done.wait()

        # Wait for first board refresh detection before the very first bump.
        # This ensures the startup bump is well-timed rather than random.
        # Give up and bump immediately after 2 * avg_interval if nothing detected.
        wait_deadline = datetime.utcnow().timestamp() + self._avg_refresh_interval * 2
        while self._last_board_refresh_ts is None:
            if datetime.utcnow().timestamp() >= wait_deadline:
                logger.info("SMART BUMP: no detection within startup window — doing initial bump now")
                break
            await asyncio.sleep(3)

        while self._enabled:
            if self._last_board_refresh_ts is not None:
                now_ts = datetime.utcnow().timestamp()
                interval = self._avg_refresh_interval
                elapsed = now_ts - self._last_board_refresh_ts
                # Always target the NEXT future refresh, even if multiple cycles have passed
                cycles_ahead = max(1, int(elapsed / interval) + 1)
                next_refresh_ts = self._last_board_refresh_ts + cycles_ahead * interval
                sleep_for = next_refresh_ts - now_ts - 1.5
                logger.info(
                    "SMART BUMP: next refresh in %.1fs (last=%.0fs ago, avg=%.0fs)",
                    sleep_for + 1.5, elapsed, interval,
                )
                if sleep_for > 1:
                    await asyncio.sleep(sleep_for)
            else:
                logger.info(
                    "SMART BUMP: no refresh detected — bumping on %.0fs fixed interval",
                    self._avg_refresh_interval,
                )

            if not self._enabled:
                break

            try:
                await self._do_smart_bumps()
            except Exception:
                logger.exception("Smart bump failed")

            # Sleep enough to avoid double-bumping in the same refresh window,
            # but short enough to recalculate timing precisely next iteration
            await asyncio.sleep(max(5.0, self._avg_refresh_interval * 0.6))

    async def _do_smart_bumps(self) -> None:
        """Bump all top_position keyword filters (called right before predicted board refresh)."""
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
                "SMART BUMP '%s' → %d lot(s) (avg_interval=%.0fs, detection=%s)",
                flt.name, min(n, len(sorted_matches)), self._avg_refresh_interval,
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
                # Determine filter cooldown from DB lots' last_bumped_at
                all_bumped = [l.last_bumped_at for l in flt.lots if l.last_bumped_at]
                filter_last_bump = max(all_bumped) if all_bumped else None
                elapsed = (now_utc - filter_last_bump).total_seconds() / 60 if filter_last_bump else float("inf")
                # Use interval-1 threshold so a 1-min filter always fires each tick
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
                filter_on_cooldown = _elapsed(lots[0]) < max(0, flt.interval_minutes - 1)
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

    async def _bump_lot(self, lot: Lot) -> None:
        try:
            cost, status_id = await self.playerok.refresh_bump_cost(
                lot.playerok_id, lot.price_kopecks / 100
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
                try:
                    cost, status_id = await self.playerok.refresh_bump_cost(
                        lot.playerok_id, lot.price_kopecks / 100
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
