import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from database.db import Database
from database.models import BumpHistory, Cycle, Filter, Lot
from playerok.client import PlayerokClient

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
        self._task = asyncio.create_task(self._run_loop())

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
        filter_candidates: list[tuple[datetime | None, Filter, list[Lot]]] = []

        for flt in filters:
            if not flt.interval_minutes:
                logger.debug("SKIP filter '%s' (id=%d): interval not set", flt.name, flt.id)
                continue
            if not self._filter_has_budget(flt):
                logger.debug("SKIP filter '%s' (id=%d): budget exhausted", flt.name, flt.id)
                continue

            eligible: list[Lot] = []
            for lot in flt.lots:
                if lot.paused:
                    continue
                if lot.last_bumped_at is not None:
                    elapsed_min = (now_utc - lot.last_bumped_at).total_seconds() / 60
                    if elapsed_min < flt.interval_minutes:
                        continue
                eligible.append(lot)

            if not eligible:
                logger.debug("SKIP filter '%s' (id=%d): no eligible lots", flt.name, flt.id)
                continue

            all_bumped = [l.last_bumped_at for l in flt.lots if l.last_bumped_at is not None]
            filter_last_bump = max(all_bumped) if all_bumped else None

            eligible.sort(key=lambda l: (l.last_bumped_at is not None, l.last_bumped_at or datetime.min, l.id))
            filter_candidates.append((filter_last_bump, flt, eligible))

        if not filter_candidates:
            logger.debug("ROBIN: no candidates this tick")
            return []

        filter_candidates.sort(key=lambda x: (x[0] is not None, x[0] or datetime.min, x[1].id))

        logger.info(
            "ROBIN candidates: %s",
            ", ".join(
                f"'{c[1].name}'(last={c[0].strftime('%H:%M:%S') if c[0] else 'never'})"
                for c in filter_candidates
            ),
        )

        _, best_flt, best_lots = filter_candidates[0]
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
            lots = sorted([l for l in flt.lots if not l.paused], key=lambda l: l.id)
            if lots:
                per_filter.append(lots)

        if not per_filter:
            return None

        schedule = self._build_cycle_schedule(per_filter, cycle.duration_minutes)

        try:
            sh, sm = (int(p) for p in cycle.start_time.split(":"))
        except ValueError:
            return None
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if start > now:
            start -= timedelta(days=1)
        elapsed_min = int((now - start).total_seconds() // 60)
        pos_in_cycle = elapsed_min % cycle.duration_minutes

        return schedule.get(pos_in_cycle)

    @staticmethod
    def _build_cycle_schedule(
        per_filter: list[list[Lot]], duration: int
    ) -> dict[int, Lot]:
        """
        Proportional schedule: the filter with the most lots fires most often.
        Example: 80r=12 lots → every 5 min; others=6 lots → every 10 min in a 60-min cycle.

        Algorithm:
        1. Primary (largest) filter occupies equally-spaced minutes.
        2. Secondary filters fill remaining minutes round-robin.
        """
        # Most lots first → primary grid
        sorted_filters = sorted(per_filter, key=lambda f: -len(f))
        schedule: dict[int, Lot] = {}

        primary = sorted_filters[0]
        primary_n = len(primary)
        primary_step = duration / primary_n
        for k, lot in enumerate(primary):
            m = round(k * primary_step) % duration
            schedule[m] = lot

        # Remaining minutes for secondary filters
        available = [m for m in range(duration) if m not in schedule]
        avail_idx = 0
        secondaries = sorted_filters[1:]
        if secondaries and available:
            max_k = max(len(f) for f in secondaries)
            for k in range(max_k):
                for flt in secondaries:
                    if k < len(flt) and avail_idx < len(available):
                        schedule[available[avail_idx]] = flt[k]
                        avail_idx += 1

        return schedule

    @staticmethod
    def _filter_has_budget(flt: Filter) -> bool:
        if flt.spend_limit_kopecks is None:
            return True
        return flt.spent_kopecks < flt.spend_limit_kopecks

    async def _bump_lot(self, lot: Lot) -> None:
        try:
            cost, status_id = await self.playerok.refresh_bump_cost(
                lot.playerok_id, lot.price_kopecks / 100
            )
        except Exception as exc:
            await self._record_failure(lot.id, f"Не удалось получить цену поднятия: {exc}")
            return

        try:
            await self.playerok.bump(lot.playerok_id, status_id)
        except Exception as exc:
            await self._record_failure(lot.id, str(exc))
            return

        await self._record_success(lot.id, cost)

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
            # Lot sold/deleted on Playerok — auto-pause so bot stops trying
            if "нельзя обновить статус" in error or "не найден" in error.lower():
                lot.paused = True
                error = "автопауза: лот продан или удалён — добавь новый в фильтр"
                logger.info("Auto-paused lot #%d: %s", lot_id, error)
            session.add(BumpHistory(lot_id=lot.id, success=False, error=error))
            await session.commit()
            await self.on_result(lot, False, 0, error)
