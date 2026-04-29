import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.keyboards.menus import (
    back_button,
    filter_card,
    filters_menu,
    lot_card,
    lots_menu,
)
from bot.states import (
    FilterAddLot,
    FilterCreate,
    FilterEditInterval,
    FilterEditLimit,
    FilterEditStartTime,
)
from database.db import Database
from database.models import Filter, Lot
from playerok.client import PlayerokClient, extract_slug

logger = logging.getLogger(__name__)
router = Router()


def _format_filter(flt: Filter) -> str:
    lines = [f"<b>Фильтр: {flt.name}</b>", ""]
    lines.append(f"Состояние: {'🟢 ВКЛ' if flt.enabled else '🔴 ВЫКЛ'}")
    lines.append(f"Лотов: {len(flt.lots)}")
    if flt.spend_limit_kopecks is not None:
        spent = flt.spent_kopecks / 100
        limit = flt.spend_limit_kopecks / 100
        lines.append(f"Лимит: {spent:.0f}₽ / {limit:.0f}₽")
    else:
        lines.append("Лимит: не задан")
    if flt.cycle_id:
        lines.append(f"Цикл: #{flt.cycle_id}")
    else:
        if flt.interval_minutes:
            lines.append(f"Интервал: каждые {flt.interval_minutes} мин")
        if flt.start_time:
            lines.append(f"Старт: {flt.start_time}")
    if flt.lots:
        lines.append("")
        total_cost = sum(l.bump_cost_kopecks for l in flt.lots) / 100
        lines.append(f"Сумма поднятия всех лотов: {total_cost:.0f}₽")
    return "\n".join(lines)


@router.callback_query(F.data == "filters")
async def cb_filters(call: CallbackQuery, db: Database) -> None:
    async with db.session_factory() as session:
        result = await session.execute(
            select(Filter).options(selectinload(Filter.lots)).order_by(Filter.order_index, Filter.id)
        )
        flts = result.scalars().all()
    await call.message.edit_text(
        "<b>Фильтры</b>\n\nКаждый фильтр = один товар (раздел Playerok).",
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
async def msg_filter_create(message: Message, state: FSMContext, db: Database) -> None:
    name = message.text.strip()[:255]
    async with db.session_factory() as session:
        flt = Filter(name=name)
        session.add(flt)
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Фильтр «{name}» создан.\n\nОткрой его и добавь лоты по URL.")


async def _open_filter(message_or_call, db: Database, filter_id: int) -> None:
    async with db.session_factory() as session:
        flt = await session.get(
            Filter, filter_id, options=[selectinload(Filter.lots)]
        )
        if flt is None:
            await message_or_call.answer("Фильтр не найден")
            return
    text = _format_filter(flt)
    kb = filter_card(flt)
    if isinstance(message_or_call, CallbackQuery):
        await message_or_call.message.edit_text(text, reply_markup=kb)
        await message_or_call.answer()
    else:
        await message_or_call.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("filter:"))
async def cb_filter_open(call: CallbackQuery, db: Database) -> None:
    fid = int(call.data.split(":")[1])
    await _open_filter(call, db, fid)


@router.callback_query(F.data.startswith("filter_toggle:"))
async def cb_filter_toggle(call: CallbackQuery, db: Database) -> None:
    fid = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid, options=[selectinload(Filter.lots)])
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


@router.callback_query(F.data.startswith("filter_addlot:"))
async def cb_filter_addlot(call: CallbackQuery, state: FSMContext) -> None:
    fid = int(call.data.split(":")[1])
    await state.set_state(FilterAddLot.waiting_for_url)
    await state.update_data(filter_id=fid)
    await call.message.edit_text(
        "Отправь ссылку на лот Playerok (https://playerok.com/products/...).\n\n"
        "Чтобы добавить несколько — отправляй по одной. Для выхода нажми «Назад».",
        reply_markup=back_button(f"filter:{fid}"),
    )
    await call.answer()


@router.message(FilterAddLot.waiting_for_url)
async def msg_filter_addlot(
    message: Message, state: FSMContext, db: Database, playerok: PlayerokClient
) -> None:
    url = message.text.strip()
    if not extract_slug(url):
        await message.answer("❌ Это не похоже на ссылку Playerok. Попробуй ещё раз.")
        return
    data = await state.get_data()
    fid = data["filter_id"]

    async with db.session_factory() as session:
        existing = await session.execute(select(Lot).where(Lot.url == url))
        if existing.scalar_one_or_none():
            await message.answer("⚠️ Этот лот уже добавлен.")
            return

    try:
        info = await playerok.get_lot_by_url(url)
    except Exception as exc:
        logger.exception("Failed to fetch lot %s", url)
        await message.answer(f"❌ Не удалось получить данные лота: {exc}")
        return

    async with db.session_factory() as session:
        flt = await session.get(Filter, fid, options=[selectinload(Filter.lots)])
        if flt is None:
            await message.answer("Фильтр не найден.")
            await state.clear()
            return
        lot = Lot(
            filter_id=fid,
            playerok_id=info.playerok_id,
            url=url,
            name=info.name,
            price_kopecks=info.price_kopecks,
            bump_cost_kopecks=info.bump_cost_kopecks,
        )
        session.add(lot)
        await session.commit()
        await session.refresh(flt, ["lots"])
        count = len(flt.lots)

    await message.answer(
        f"✅ Лот добавлен.\n\n"
        f"<b>{info.name}</b>\n"
        f"Цена: {info.price_kopecks/100:.0f}₽\n"
        f"Стоимость поднятия: {info.bump_cost_kopecks/100:.0f}₽\n\n"
        f"Всего в фильтре: {count} лот(а/ов).\n\n"
        "Отправь ещё одну ссылку или нажми «Назад»."
    )


@router.callback_query(F.data.startswith("filter_lots:"))
async def cb_filter_lots(call: CallbackQuery, db: Database) -> None:
    fid = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid, options=[selectinload(Filter.lots)])
        if flt is None:
            await call.answer("Не найдено")
            return
    if not flt.lots:
        await call.message.edit_text(
            "В этом фильтре пока нет лотов.",
            reply_markup=back_button(f"filter:{fid}"),
        )
    else:
        await call.message.edit_text(
            f"<b>Лоты ({len(flt.lots)})</b>",
            reply_markup=lots_menu(fid, flt.lots),
        )
    await call.answer()


@router.callback_query(F.data.startswith("lot:"))
async def cb_lot_open(call: CallbackQuery, db: Database) -> None:
    lot_id = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        lot = await session.get(Lot, lot_id)
        if lot is None:
            await call.answer("Не найдено")
            return
    text = (
        f"<b>{lot.name}</b>\n\n"
        f"<a href=\"{lot.url}\">Открыть на Playerok</a>\n"
        f"Цена: {lot.price_kopecks/100:.0f}₽\n"
        f"Поднятие: {lot.bump_cost_kopecks/100:.0f}₽\n"
        f"Состояние: {'⏸ на паузе' if lot.paused else '▶️ активен'}\n"
        f"Последнее поднятие: "
        f"{lot.last_bumped_at.strftime('%Y-%m-%d %H:%M') if lot.last_bumped_at else '—'}"
    )
    await call.message.edit_text(text, reply_markup=lot_card(lot), disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data.startswith("lot_pause:"))
async def cb_lot_pause(call: CallbackQuery, db: Database) -> None:
    lot_id = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        lot = await session.get(Lot, lot_id)
        if lot:
            lot.paused = not lot.paused
            await session.commit()
    await cb_lot_open(call, db)


@router.callback_query(F.data.startswith("lot_delete:"))
async def cb_lot_delete(call: CallbackQuery, db: Database) -> None:
    lot_id = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        lot = await session.get(Lot, lot_id)
        fid = lot.filter_id if lot else None
        if lot:
            await session.delete(lot)
            await session.commit()
    await call.answer("Удалено")
    if fid:
        await _open_filter(call, db, fid)


@router.callback_query(F.data.startswith("filter_limit:"))
async def cb_filter_limit(call: CallbackQuery, state: FSMContext) -> None:
    fid = int(call.data.split(":")[1])
    await state.set_state(FilterEditLimit.waiting_for_amount)
    await state.update_data(filter_id=fid)
    await call.message.edit_text(
        "Введи лимит расходов в рублях (например 864), или 0 чтобы убрать лимит:",
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
    await message.answer(
        "✅ Лимит сохранён." if rub > 0 else "✅ Лимит снят."
    )


@router.callback_query(F.data.startswith("filter_interval:"))
async def cb_filter_interval(call: CallbackQuery, state: FSMContext) -> None:
    fid = int(call.data.split(":")[1])
    await state.set_state(FilterEditInterval.waiting_for_minutes)
    await state.update_data(filter_id=fid)
    await call.message.edit_text(
        "Введи интервал в минутах (например 30). 0 — отключить независимый режим.",
        reply_markup=back_button(f"filter:{fid}"),
    )
    await call.answer()


@router.message(FilterEditInterval.waiting_for_minutes)
async def msg_filter_interval(message: Message, state: FSMContext, db: Database) -> None:
    try:
        minutes = int(message.text.strip())
    except ValueError:
        await message.answer("Нужно число.")
        return
    data = await state.get_data()
    fid = data["filter_id"]
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            flt.interval_minutes = minutes if minutes > 0 else None
            await session.commit()
    await state.clear()
    await message.answer("✅ Интервал сохранён.")


@router.callback_query(F.data.startswith("filter_start:"))
async def cb_filter_start(call: CallbackQuery, state: FSMContext) -> None:
    fid = int(call.data.split(":")[1])
    await state.set_state(FilterEditStartTime.waiting_for_time)
    await state.update_data(filter_id=fid)
    await call.message.edit_text(
        "Введи время старта в формате HH:MM (например 14:00). Пусто/«-» — без привязки.",
        reply_markup=back_button(f"filter:{fid}"),
    )
    await call.answer()


@router.message(FilterEditStartTime.waiting_for_time)
async def msg_filter_start(message: Message, state: FSMContext, db: Database) -> None:
    text = message.text.strip()
    value: str | None
    if text in ("-", ""):
        value = None
    else:
        try:
            h, m = (int(p) for p in text.split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
            value = f"{h:02d}:{m:02d}"
        except (ValueError, AssertionError):
            await message.answer("Неверный формат. Пример: 14:00")
            return
    data = await state.get_data()
    fid = data["filter_id"]
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            flt.start_time = value
            await session.commit()
    await state.clear()
    await message.answer("✅ Время старта сохранено.")
