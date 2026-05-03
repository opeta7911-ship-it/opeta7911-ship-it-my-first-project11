import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import desc, select, update


from bot.keyboards.menus import (
    back_button,
    filter_card,
    filter_cycle_assign_menu,
    filters_menu,
    interval_menu,
    lots_per_trigger_menu,
)
from bot.states import FilterCreate, FilterEditIntervalCustom, FilterEditKeyword, FilterEditLimit
from database.db import Database
from database.models import BumpHistory, Cycle, Filter, Lot

logger = logging.getLogger(__name__)
router = Router()


def _format_filter(flt: Filter) -> str:
    lines = [f"<b>Фильтр: {flt.name}</b>", ""]
    lines.append(f"Состояние: {'🟢 ВКЛ' if flt.enabled else '🔴 ВЫКЛ'}")
    if flt.keyword:
        lines.append(f"🔑 Ключевое слово: <code>{flt.keyword}</code>")
    else:
        lines.append("🔑 Ключевое слово: ⚠️ не задано")
    if flt.cycle_id:
        lines.append("Поднимать: ✅ настроено циклом")
    elif flt.interval_minutes:
        lines.append(f"Поднимать: каждые {flt.interval_minutes} мин")
    else:
        lines.append("Поднимать: ⚠️ не настроено")
    lines.append(f"Лотов за раз: {flt.lots_per_trigger}")
    if flt.spend_limit_kopecks is not None:
        from datetime import timedelta
        if flt.limit_reset_at:
            reset_at = flt.limit_reset_at + timedelta(hours=24)
            reset_info = f" · сброс в {reset_at.strftime('%H:%M')}"
        else:
            reset_info = ""
        lines.append(f"Лимит/сутки: {flt.spent_kopecks//100}₽ / {flt.spend_limit_kopecks//100}₽{reset_info}")
    else:
        lines.append("Лимит/сутки: нет")
    if flt.cycle_id:
        lines.append(f"Цикл: #{flt.cycle_id}")
    return "\n".join(lines)


async def _open_filter(call: CallbackQuery, db: Database, filter_id: int) -> None:
    async with db.session_factory() as session:
        flt = await session.get(Filter, filter_id)
        if flt is None:
            await call.answer("Фильтр не найден", show_alert=True)
            return
    try:
        await call.message.edit_text(
            _format_filter(flt),
            reply_markup=filter_card(flt),
            disable_web_page_preview=True,
        )
    except Exception:
        pass  # message already has same content


async def _send_filter_card(message: Message, db: Database, filter_id: int) -> None:
    async with db.session_factory() as session:
        flt = await session.get(Filter, filter_id)
        if flt is None:
            return
    await message.answer(
        _format_filter(flt),
        reply_markup=filter_card(flt),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "filters")
async def cb_filters(call: CallbackQuery, db: Database) -> None:
    await call.answer()
    async with db.session_factory() as session:
        result = await session.execute(
            select(Filter).order_by(Filter.order_index, Filter.id)
        )
        flts = result.scalars().all()
    await call.message.edit_text(
        "<b>Фильтры</b>\n\nКаждый фильтр — один набор лотов с общими настройками поднятия.",
        reply_markup=filters_menu(flts),
    )


@router.callback_query(F.data == "filter_create")
async def cb_filter_create(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(FilterCreate.waiting_for_name)
    await call.message.edit_text(
        "Введи название фильтра (например, «80 Робуксов»):",
        reply_markup=back_button("filters"),
    )


@router.message(FilterCreate.waiting_for_name)
async def msg_filter_name(message: Message, state: FSMContext, db: Database) -> None:
    name = message.text.strip()[:255]
    async with db.session_factory() as session:
        flt = Filter(name=name)
        session.add(flt)
        await session.commit()
        new_id = flt.id
    await state.clear()
    await _send_filter_card(message, db, new_id)


@router.callback_query(F.data.startswith("filter:"))
async def cb_filter_open(call: CallbackQuery, db: Database, state: FSMContext) -> None:
    await call.answer()
    fid = int(call.data.split(":")[1])
    await _open_filter(call, db, fid)


@router.callback_query(F.data.startswith("filter_toggle:"))
async def cb_filter_toggle(call: CallbackQuery, db: Database) -> None:
    await call.answer()
    fid = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            flt.enabled = not flt.enabled
            await session.commit()
    await _open_filter(call, db, fid)


@router.callback_query(F.data.startswith("filter_delete:"))
async def cb_filter_delete(call: CallbackQuery, db: Database) -> None:
    await call.answer("Удалено")
    fid = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            await session.delete(flt)
            await session.commit()
    await cb_filters(call, db)


# ── Ключевое слово ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_keyword:"))
async def cb_filter_keyword(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    fid = int(call.data.split(":")[1])
    await state.set_state(FilterEditKeyword.waiting_for_keyword)
    await state.update_data(filter_id=fid)
    await call.message.edit_text(
        "Введи ключевое слово для поиска лотов на Playerok.\n\n"
        "Бот будет каждую минуту искать все твои активные лоты, у которых это слово "
        "есть в названии, и поднимать <b>самый старый</b> из них автоматически.\n\n"
        "Пример: <code>352</code> или <code>88 Робуксов</code>\n\n"
        "Отправь <code>0</code>, чтобы убрать ключевое слово:",
        reply_markup=back_button(f"filter:{fid}"),
    )


@router.message(FilterEditKeyword.waiting_for_keyword)
async def msg_filter_keyword(message: Message, state: FSMContext, db: Database) -> None:
    kw = message.text.strip()
    data = await state.get_data()
    fid = data["filter_id"]
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            flt.keyword = None if kw == "0" else kw[:255]
            await session.commit()
    await state.clear()
    await _send_filter_card(message, db, fid)


# ── Интервал поднятия ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_interval_menu:"))
async def cb_filter_interval_menu(call: CallbackQuery) -> None:
    await call.answer()
    fid = int(call.data.split(":")[1])
    await call.message.edit_text(
        "Как часто поднимать лоты?",
        reply_markup=interval_menu(fid),
    )


@router.callback_query(F.data.startswith("filter_interval_set:"))
async def cb_filter_interval_set(call: CallbackQuery, db: Database) -> None:
    parts = call.data.split(":")
    fid, minutes = int(parts[1]), int(parts[2])
    await call.answer("✅ Сохранено")
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            flt.interval_minutes = minutes
            await session.commit()
    await _open_filter(call, db, fid)


@router.callback_query(F.data.startswith("filter_interval_custom:"))
async def cb_filter_interval_custom(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    fid = int(call.data.split(":")[1])
    await state.set_state(FilterEditIntervalCustom.waiting_for_minutes)
    await state.update_data(filter_id=fid)
    await call.message.edit_text(
        "Введи интервал в минутах (например, 45):",
        reply_markup=back_button(f"filter:{fid}"),
    )


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
    await _send_filter_card(message, db, fid)


# ── Лотов за раз ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_lpt_menu:"))
async def cb_filter_lpt_menu(call: CallbackQuery) -> None:
    await call.answer()
    fid = int(call.data.split(":")[1])
    await call.message.edit_text(
        "Сколько лотов поднимать за одно срабатывание?",
        reply_markup=lots_per_trigger_menu(fid),
    )


@router.callback_query(F.data.startswith("filter_lpt_set:"))
async def cb_filter_lpt_set(call: CallbackQuery, db: Database) -> None:
    parts = call.data.split(":")
    fid, n = int(parts[1]), int(parts[2])
    await call.answer("✅ Сохранено")
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            flt.lots_per_trigger = n
            await session.commit()
    await _open_filter(call, db, fid)


# ── Лимит расходов ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_limit:"))
async def cb_filter_limit(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    fid = int(call.data.split(":")[1])
    await state.set_state(FilterEditLimit.waiting_for_amount)
    await state.update_data(filter_id=fid)
    await call.message.edit_text(
        "Введи суточный лимит расходов в рублях (например 864).\n"
        "Лимит сбрасывается каждый день в 12:00 по местному времени.\n"
        "0 — убрать лимит:",
        reply_markup=back_button(f"filter:{fid}"),
    )


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
    await _send_filter_card(message, db, fid)


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
    await call.answer()
    await call.message.edit_text(
        "Выбери цикл для этого фильтра:",
        reply_markup=filter_cycle_assign_menu(fid, cycles, flt.cycle_id if flt else None),
    )


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


LOGS_PAGE_SIZE = 10


@router.callback_query(F.data.startswith("filter_logs:"))
async def cb_filter_logs(call: CallbackQuery, db: Database) -> None:
    await call.answer()
    parts = call.data.split(":")
    fid, page = int(parts[1]), int(parts[2])

    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if not flt:
            return

        total_q = await session.execute(
            select(BumpHistory)
            .join(Lot, BumpHistory.lot_id == Lot.id)
            .where(Lot.filter_id == fid)
        )
        total = len(total_q.scalars().all())

        result = await session.execute(
            select(BumpHistory, Lot)
            .join(Lot, BumpHistory.lot_id == Lot.id)
            .where(Lot.filter_id == fid)
            .order_by(desc(BumpHistory.occurred_at))
            .offset(page * LOGS_PAGE_SIZE)
            .limit(LOGS_PAGE_SIZE)
        )
        rows = result.all()

    if not rows:
        try:
            await call.message.edit_text(
                f"<b>Логи фильтра «{flt.name}»</b>\n\nПоднятий ещё не было.",
                reply_markup=back_button(f"filter:{fid}"),
            )
        except Exception:
            pass
        return

    lines = [f"<b>Логи фильтра «{flt.name}»</b>  (стр. {page+1})\n"]
    for hist, lot in rows:
        dt = hist.occurred_at.strftime("%d.%m %H:%M")
        icon = "✅" if hist.success else "❌"
        cost = f" {hist.cost_kopecks // 100}₽" if hist.success and hist.cost_kopecks else ""
        lines.append(f'{icon} {dt}{cost}\n└ <a href="{lot.url}">{lot.name[:40]}</a>')

    from aiogram.types import InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"filter_logs:{fid}:{page-1}"))
    pages = (total + LOGS_PAGE_SIZE - 1) // LOGS_PAGE_SIZE
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
    if (page + 1) * LOGS_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"filter_logs:{fid}:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data=f"filter:{fid}"))

    try:
        await call.message.edit_text(
            "\n".join(lines),
            reply_markup=kb.as_markup(),
            disable_web_page_preview=True,
        )
    except Exception:
        pass


@router.callback_query(F.data == "filters_disable_all")
async def cb_filters_disable_all(call: CallbackQuery, db: Database) -> None:
    await call.answer("⛔ Все фильтры выключены")
    async with db.session_factory() as session:
        await session.execute(update(Filter).values(enabled=False))
        await session.commit()
        result = await session.execute(select(Filter).order_by(Filter.order_index, Filter.id))
        flts = result.scalars().all()
    await call.message.edit_reply_markup(reply_markup=filters_menu(flts))


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()
