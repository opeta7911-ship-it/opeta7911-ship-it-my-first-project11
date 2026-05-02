import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from database.db import Database
from database.models import BumpHistory, Cycle, Filter, Lot
from playerok.client import MyLot, PlayerokClient

logger = logging.getLogger(__name__)

BumpCallback = Callable[[Lot, bool, int, str | None], Awaitable[None]]


class DailyResetTask:
    """Сбрасывает потраченный бюджет фильтров каждый день в 12:00 по местному времени."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            await self._sleep_until_noon()
            await self._reset_budgets()

    async def _sleep_until_noon(self) -> None:
        now = datetime.now()
        noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if now >= noon:
            noon += timedelta(days=1)
        await asyncio.sleep((noon - now).total_seconds())

    async def _reset_budgets(self) -> None:
        now = datetime.now()
        async with self.db.session_factory() as session:
            await session.execute(
                update(Filter)
                .where(Filter.spend_limit_kopecks.isnot(None))
                .values(spent_kopecks=0, limit_reset_at=now)
            )
            await session.commit()
        logger.info("Daily limit reset executed at %s", now.strftime("%H:%M"))


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
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._enabled = True
        asyncio.create_task(self._startup_sync())
        self._task = asyncio.create_task(self._run_loop())

    async def _startup_sync(self) -> None:
        """On startup, sync all stored lots against currently active Playerok lots."""
        try:
            active = await self.playerok.get_my_lots()
        except Exception as e:
            logger.warning("Startup sync: get_my_lots failed: %s", e)
            return

        active_ids = {a.playerok_id for a in active}

        async with self.db.session_factory() as session:
            result = await session.execute(select(Lot).where(Lot.paused.is_(False)))
            all_lots = result.scalars().all()

        active_by_id = {a.playerok_id: a for a in active}
        untracked = [a for a in active if a.playerok_id not in {l.playerok_id for l in all_lots}]
        updated = 0

        # Refresh expires_at for lots already tracked and still active
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
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

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
        await asyncio.sleep(max(0.0, (next_minute - now).total_seconds()))

    async def _tick(self) -> None:
        now = datetime.now()
        now_utc = datetime.utcnow()
        lots = await self._pick_next_lots(now, now_utc)
        if not lots:
            logger.debug("TICK %s — no lot selected", now.strftime("%H:%M"))
        for lot in lots:
            await self._bump_lot(lot)

    async def _pick_next_lots(self, now: datetime, now_utc: datetime) -> list[Lot]:
        result: list[Lot] = []
        async with self.db.session_factory() as session:
            # --- Циклы (логика без изменений) ---
            cycles_q = await session.execute(
                select(Cycle)
                .where(Cycle.enabled.is_(True))
                .options(selectinload(Cycle.filters).selectinload(Filter.lots))
            )
            for cycle in cycles_q.scalars().all():
                lot = self._pick_from_cycle(cycle, now)
                if lot is not None:
                    result.append(lot)

            # --- Независимые фильтры: глобальный раунд-робин ---
            indep_q = await session.execute(
                select(Filter)
                .where(Filter.cycle_id.is_(None), Filter.enabled.is_(True))
                .options(selectinload(Filter.lots))
            )
            global_pick = self._pick_independent_global(indep_q.scalars().all(), now_utc)
            result.extend(global_pick)

        return result

    def _pick_independent_global(self, filters: list[Filter], now_utc: datetime) -> list[Lot]:
        # Each entry: (filter_on_cooldown, filter_last_bump, flt, sorted_lots)
        filter_candidates = []

        for flt in filters:
            if not flt.interval_minutes:
                logger.debug("SKIP filter '%s' (id=%d): interval not set", flt.name, flt.id)
                continue
            if not self._filter_has_budget(flt):
                logger.debug("SKIP filter '%s' (id=%d): budget exhausted", flt.name, flt.id)
                continue

            lots = [l for l in flt.lots if not l.paused]
            if not lots:
                logger.debug("SKIP filter '%s' (id=%d): no lots", flt.name, flt.id)
                continue

            def _elapsed(l: Lot) -> float:
                return (now_utc - l.last_bumped_at).total_seconds() / 60 if l.last_bumped_at else float("inf")

            def _lot_key(l: Lot):
                on_cd = _elapsed(l) < flt.interval_minutes
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
            # Filter is "on cooldown" when its best available lot is still within interval
            filter_on_cooldown = _elapsed(lots[0]) < flt.interval_minutes

            filter_candidates.append((filter_on_cooldown, filter_last_bump, flt, lots))

        if not filter_candidates:
            logger.debug("ROBIN: no candidates this tick")
            return []

        # Non-cooldown filters first; among equal cooldown state, oldest filter_last_bump first
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

        _, _, best_flt, best_lots = filter_candidates[0]
        n = best_flt.lots_per_trigger or 1
        logger.info("ROBIN selected: '%s' → %d lot(s)", best_flt.name, n)
        return best_lots[:n]

    def _pick_from_cycle(self, cycle: Cycle, now: datetime) -> Lot | None:
        active_filters = sorted(
            [f for f in cycle.filters if f.enabled],
            key=lambda f: f.order_index,
        )
        per_filter: list[list[Lot]] = []
        for flt in active_filters:
            if not self._filter_has_budget(flt):
                continue
            lots = [l for l in flt.lots if not l.paused]
            if lots:
                per_filter.append(lots)

        if not per_filter:
            return None

        # minute → all lots for the filter that fires at that minute
        schedule = self._build_cycle_schedule(per_filter, cycle.duration_minutes)
        total_lots = sum(len(f) for f in per_filter)
        filled = len(schedule)
        logger.info(
            "CYCLE '%s': %d filters, %d lots, %d/%d min filled",
            cycle.name, len(per_filter), total_lots, filled, cycle.duration_minutes,
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

        filter_lots = schedule.get(pos_in_cycle)
        if not filter_lots:
            logger.debug("CYCLE '%s' pos=%d — empty slot", cycle.name, pos_in_cycle)
            return None

        lot = min(
            filter_lots,
            key=lambda l: (l.expires_at is None, l.expires_at or datetime.max, l.id),
        )
        logger.info(
            "CYCLE '%s' pos=%d — lot #%d expires=%s",
            cycle.name, pos_in_cycle, lot.id,
            lot.expires_at.strftime("%Y-%m-%d %H:%M") if lot.expires_at else "unknown",
        )
        return lot

    @staticmethod
    def _build_cycle_schedule(
        per_filter: list[list[Lot]], duration: int
    ) -> dict[int, list[Lot]]:
        """
        Assigns each filter a set of evenly-spaced minutes using greedy offset search.
        Filters with more lots fire more often and claim slots first.
        No two filters ever share the same minute.
        """
        sorted_filters = sorted(per_filter, key=lambda f: -len(f))
        schedule: dict[int, list[Lot]] = {}

        for flt_lots in sorted_filters:
            n = min(len(flt_lots), duration)
            step = duration / n
            for offset in range(duration):
                positions = [round(offset + k * step) % duration for k in range(n)]
                if len(set(positions)) == n and all(p not in schedule for p in positions):
                    for p in positions:
                        schedule[p] = flt_lots
                    break

        return schedule

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
                    # Re-listing bot may not have re-listed yet — skip this tick silently
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
        # 1. Exact normalized name
        by_name = [c for c in candidates if BumpEngine._normalize(c.name) == norm]
        pool = by_name if by_name else candidates
        # 2. Closest price within 10%
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
            lot.last_bumped_at = datetime.utcnow()
            lot.bump_cost_kopecks = cost_kopecks
            lot.filter.spent_kopecks += cost_kopecks
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
