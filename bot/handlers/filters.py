import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.keyboards.menus import (
    back_button,
    filter_card,
    filter_cycle_assign_menu,
    filters_menu,
    interval_menu,
    lot_selection_menu,
    lots_per_trigger_menu,
)
from bot.states import FilterCreate, FilterEditIntervalCustom, FilterEditLimit
from database.db import Database
from database.models import Cycle, Filter, Lot
from playerok.client import PlayerokClient

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 8


def _format_filter(flt: Filter) -> str:
    lines = [f"<b>Фильтр: {flt.name}</b>", ""]
    lines.append(f"Состояние: {'🟢 ВКЛ' if flt.enabled else '🔴 ВЫКЛ'}")
    lines.append(f"Лотов: {len(flt.lots)}")
    if flt.interval_minutes:
        lines.append(f"Поднимать: каждые {flt.interval_minutes} мин")
    else:
        lines.append("Поднимать: ⚠️ не настроено")
    lines.append(f"Лотов за раз: {flt.lots_per_trigger}")
    if flt.spend_limit_kopecks is not None:
        reset_info = f" · сброс в 12:00" if flt.limit_reset_at else ""
        lines.append(f"Лимит/сутки: {flt.spent_kopecks//100}₽ / {flt.spend_limit_kopecks//100}₽{reset_info}")
    else:
        lines.append("Лимит/сутки: нет")
    if flt.cycle_id:
        lines.append(f"Цикл: #{flt.cycle_id}")
    return "\n".join(lines)


async def _open_filter(call: CallbackQuery, db: Database, filter_id: int) -> None:
    async with db.session_factory() as session:
        flt = await session.get(Filter, filter_id, options=[selectinload(Filter.lots)])
        if flt is None:
            await call.answer("Фильтр не найден")
            return
    await call.message.edit_text(_format_filter(flt), reply_markup=filter_card(flt))
    await call.answer()


@router.callback_query(F.data == "filters")
async def cb_filters(call: CallbackQuery, db: Database) -> None:
    async with db.session_factory() as session:
        result = await session.execute(
            select(Filter).options(selectinload(Filter.lots)).order_by(Filter.order_index, Filter.id)
        )
        flts = result.scalars().all()
    await call.message.edit_text(
        "<b>Фильтры</b>\n\nКаждый фильтр — один набор лотов с общими настройками поднятия.",
        reply_markup=filters_menu(flts),
    )
    await call.answer()


@router.callback_query(F.data == "filter_create")
async def cb_filter_create(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FilterCreate.waiting_for_name)
    await call.message.edit_text(
        "Введи название фильтра (например, «80 Робуксов»):",
        reply_markup=back_button("filters"),
    )
    await call.answer()


@router.message(FilterCreate.waiting_for_name)
async def msg_filter_name(message: Message, state: FSMContext, db: Database) -> None:
    name = message.text.strip()[:255]
    async with db.session_factory() as session:
        flt = Filter(name=name)
        session.add(flt)
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Фильтр «{name}» создан.\nТеперь выбери лоты через «📋 Выбрать лоты».")


@router.callback_query(F.data.startswith("filter:"))
async def cb_filter_open(call: CallbackQuery, db: Database) -> None:
    fid = int(call.data.split(":")[1])
    await _open_filter(call, db, fid)


@router.callback_query(F.data.startswith("filter_toggle:"))
async def cb_filter_toggle(call: CallbackQuery, db: Database) -> None:
    fid = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            flt.enabled = not flt.enabled
            await session.commit()
    await _open_filter(call, db, fid)


@router.callback_query(F.data.startswith("filter_delete:"))
async def cb_filter_delete(call: CallbackQuery, db: Database) -> None:
    fid = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            await session.delete(flt)
            await session.commit()
    await call.answer("Удалено")
    await cb_filters(call, db)


# ── Выбор лотов из списка ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_lots_fetch:"))
async def cb_filter_lots_fetch(
    call: CallbackQuery, db: Database, playerok: PlayerokClient, state: FSMContext
) -> None:
    parts = call.data.split(":")
    fid, page = int(parts[1]), int(parts[2])

    await call.message.edit_text("⏳ Загружаю лоты с Playerok...")
    await call.answer()

    try:
        all_lots = await playerok.get_my_lots()
    except Exception as exc:
        await call.message.edit_text(
            f"❌ Не удалось загрузить лоты: {exc}",
            reply_markup=back_button(f"filter:{fid}"),
        )
        return

    if not all_lots:
        await call.message.edit_text(
            "На Playerok нет активных лотов.",
            reply_markup=back_button(f"filter:{fid}"),
        )
        return

    # Кэшируем список лотов в FSM, чтобы не перезапрашивать при каждом нажатии
    await state.update_data(
        lots_cache=[
            {
                "playerok_id": l.playerok_id,
                "slug": l.slug,
                "name": l.name,
                "price_kopecks": l.price_kopecks,
            }
            for l in all_lots
        ]
    )

    async with db.session_factory() as session:
        flt = await session.get(Filter, fid, options=[selectinload(Filter.lots)])
        if not flt:
            await call.message.edit_text("Фильтр не найден.")
            return
        selected_ids = {lot.playerok_id for lot in flt.lots}

    start = page * PAGE_SIZE
    page_lots = all_lots[start: start + PAGE_SIZE]

    await call.message.edit_text(
        f"<b>Выбери лоты для фильтра «{flt.name}»</b>\n"
        f"Выбрано: {len(selected_ids)} из {len(all_lots)}",
        reply_markup=lot_selection_menu(fid, page_lots, selected_ids, page, len(all_lots), PAGE_SIZE),
    )


@router.callback_query(F.data.startswith("lot_toggle:"))
async def cb_lot_toggle(
    call: CallbackQuery, db: Database, playerok: PlayerokClient, state: FSMContext
) -> None:
    parts = call.data.split(":")
    fid, playerok_id, page = int(parts[1]), parts[2], int(parts[3])

    # Мгновенно обновляем клавиатуру из кэша — без лишних запросов к Playerok
    fsm_data = await state.get_data()
    cached = fsm_data.get("lots_cache", [])

    async with db.session_factory() as session:
        flt = await session.get(Filter, fid, options=[selectinload(Filter.lots)])
        if not flt:
            await call.answer()
            return

        existing = next((l for l in flt.lots if l.playerok_id == playerok_id), None)
        if existing:
            await session.delete(existing)
        else:
            lot_info = next((l for l in cached if l["playerok_id"] == playerok_id), None)
            if lot_info:
                try:
                    cost, _ = await playerok.get_lot_bump_cost(
                        lot_info["playerok_id"], lot_info["price_kopecks"] / 100
                    )
                except Exception:
                    logger.exception("Failed to fetch bump cost for %s", playerok_id)
                    cost = 0
                from playerok.client import BASE_URL
                session.add(Lot(
                    filter_id=fid,
                    playerok_id=lot_info["playerok_id"],
                    url=f"{BASE_URL}{lot_info['slug']}",
                    name=lot_info["name"],
                    price_kopecks=lot_info["price_kopecks"],
                    bump_cost_kopecks=cost,
                ))
        await session.commit()
        await session.refresh(flt, ["lots"])
        selected_ids = {l.playerok_id for l in flt.lots}

    # Если кэш есть — перерисовываем клавиатуру мгновенно, без API-запроса
    if cached:
        from playerok.client import MyLot
        all_lots = [
            MyLot(
                playerok_id=l["playerok_id"],
                slug=l["slug"],
                name=l["name"],
                price_kopecks=l["price_kopecks"],
                bump_cost_kopecks=0,
                bump_priority_status_id="",
            )
            for l in cached
        ]
    else:
        try:
            all_lots = await playerok.get_my_lots()
        except Exception:
            all_lots = []

    start = page * PAGE_SIZE
    page_lots = all_lots[start: start + PAGE_SIZE]
    await call.message.edit_reply_markup(
        reply_markup=lot_selection_menu(fid, page_lots, selected_ids, page, len(all_lots), PAGE_SIZE)
    )
    await call.answer()


# ── Интервал поднятия ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_interval_menu:"))
async def cb_filter_interval_menu(call: CallbackQuery) -> None:
    fid = int(call.data.split(":")[1])
    await call.message.edit_text(
        "Как часто поднимать лоты?",
        reply_markup=interval_menu(fid),
    )
    await call.answer()


@router.callback_query(F.data.startswith("filter_interval_set:"))
async def cb_filter_interval_set(call: CallbackQuery, db: Database) -> None:
    parts = call.data.split(":")
    fid, minutes = int(parts[1]), int(parts[2])
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            flt.interval_minutes = minutes
            await session.commit()
    await call.answer("✅ Сохранено")
    await _open_filter(call, db, fid)


@router.callback_query(F.data.startswith("filter_interval_custom:"))
async def cb_filter_interval_custom(call: CallbackQuery, state: FSMContext) -> None:
    fid = int(call.data.split(":")[1])
    await state.set_state(FilterEditIntervalCustom.waiting_for_minutes)
    await state.update_data(filter_id=fid)
    await call.message.edit_text(
        "Введи интервал в минутах (например, 45):",
        reply_markup=back_button(f"filter:{fid}"),
    )
    await call.answer()


@router.message(FilterEditIntervalCustom.waiting_for_minutes)
async def msg_interval_custom(message: Message, state: FSMContext, db: Database) -> None:
    try:
        minutes = int(message.text.strip())
        assert minutes > 0
    except (ValueError, AssertionError):
        await message.answer("Нужно положительное число.")
        return
    data = await state.get_data()
    fid = data["filter_id"]
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            flt.interval_minutes = minutes
            await session.commit()
    await state.clear()
    await message.answer(f"✅ Интервал: каждые {minutes} мин.")


# ── Лотов за раз ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_lpt_menu:"))
async def cb_filter_lpt_menu(call: CallbackQuery) -> None:
    fid = int(call.data.split(":")[1])
    await call.message.edit_text(
        "Сколько лотов поднимать за одно срабатывание?",
        reply_markup=lots_per_trigger_menu(fid),
    )
    await call.answer()


@router.callback_query(F.data.startswith("filter_lpt_set:"))
async def cb_filter_lpt_set(call: CallbackQuery, db: Database) -> None:
    parts = call.data.split(":")
    fid, n = int(parts[1]), int(parts[2])
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            flt.lots_per_trigger = n
            await session.commit()
    await call.answer("✅ Сохранено")
    await _open_filter(call, db, fid)


# ── Лимит расходов ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_limit:"))
async def cb_filter_limit(call: CallbackQuery, state: FSMContext) -> None:
    fid = int(call.data.split(":")[1])
    await state.set_state(FilterEditLimit.waiting_for_amount)
    await state.update_data(filter_id=fid)
    await call.message.edit_text(
        "Введи суточный лимит расходов в рублях (например 864).\n"
        "Лимит сбрасывается каждый день в 12:00 по местному времени.\n"
        "0 — убрать лимит:",
        reply_markup=back_button(f"filter:{fid}"),
    )
    await call.answer()


@router.message(FilterEditLimit.waiting_for_amount)
async def msg_filter_limit(message: Message, state: FSMContext, db: Database) -> None:
    try:
        rub = int(message.text.strip())
    except ValueError:
        await message.answer("Нужно число.")
        return
    data = await state.get_data()
    fid = data["filter_id"]
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            flt.spend_limit_kopecks = rub * 100 if rub > 0 else None
            flt.spent_kopecks = 0
            await session.commit()
    await state.clear()
    await message.answer("✅ Лимит сохранён." if rub > 0 else "✅ Лимит снят.")


# ── Назначение цикла ───────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_cycle_assign:"))
async def cb_filter_cycle_assign(call: CallbackQuery, db: Database) -> None:
    fid = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        result = await session.execute(select(Cycle).order_by(Cycle.id))
        cycles = result.scalars().all()
    if not cycles:
        await call.answer("Сначала создай цикл в разделе «Циклы».", show_alert=True)
        return
    await call.message.edit_text(
        "Выбери цикл для этого фильтра:",
        reply_markup=filter_cycle_assign_menu(fid, cycles, flt.cycle_id if flt else None),
    )
    await call.answer()


@router.callback_query(F.data.startswith("filter_cycle_set:"))
async def cb_filter_cycle_set(call: CallbackQuery, db: Database) -> None:
    parts = call.data.split(":")
    fid, cid = int(parts[1]), int(parts[2])
    lpt_reset = False
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            flt.cycle_id = cid if cid > 0 else None
            if cid > 0 and flt.lots_per_trigger > 1:
                flt.lots_per_trigger = 1
                lpt_reset = True
            await session.commit()
    if lpt_reset:
        await call.answer(
            "⚠️ В цикле можно поднимать только 1 лот за раз.\n"
            "«Лотов за раз» автоматически изменено на 1.",
            show_alert=True,
        )
    else:
        await call.answer("✅ Сохранено")
    await _open_filter(call, db, fid)


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()
