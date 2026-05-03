from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.keyboards.menus import back_button, cycle_card, cycles_menu
from bot.states import CycleCreate, CycleEditDuration, CycleEditStart
from database.db import Database
from database.models import Cycle, Filter

router = Router()


def _format_cycle(cycle: Cycle, all_filters: list[Filter], live_lots: list) -> str:
    in_cycle = [f for f in cycle.filters if f.enabled]
    dur = cycle.duration_minutes

    lines = [
        f"<b>Цикл: {cycle.name}</b>",
        "",
        f"Состояние: {'🟢 ВКЛ' if cycle.enabled else '🔴 ВЫКЛ'}",
        f"Старт: {cycle.start_time}  |  Длительность: {dur} мин",
        "",
    ]

    if in_cycle:
        lines.append("📅 <b>Как работает сейчас:</b>")
        total_slots = 0
        for flt in in_cycle:
            if flt.keyword:
                kw = flt.keyword.lower()
                n = len([l for l in live_lots if kw in l.name.lower()])
            else:
                n = len([l for l in flt.lots if not l.paused])

            if n == 0:
                lines.append(f"  ⚠️ {flt.name} — лотов нет, пропускается")
                continue

            slots = min(n, dur)
            interval = dur // slots
            total_slots += slots
            lines.append(
                f"  • <b>{flt.name}</b> — {n} лот(а) "
                f"→ поднимается каждые {interval} мин ({slots} раз за {dur} мин)"
            )

        filled = min(total_slots, dur)
        empty = dur - filled
        lines.append("")
        lines.append(
            f"Заполнено: {filled}/{dur} мин"
            + (f" (пустых слотов: {empty})" if empty else " ✅ полностью")
        )

        lines.append("")
        lines.append(
            "💡 <b>Как это работает:</b> Бот поднимает лоты каждую минуту по расписанию. "
            "Каждый фильтр получает минуты пропорционально числу лотов. "
            "В каждую свою минуту бот берёт самый старый лот фильтра (ближайший к истечению) и поднимает его."
        )
    else:
        lines.append(
            "Фильтров нет. Отметь галочкой ниже, чтобы добавить фильтр в цикл.\n\n"
            "💡 <b>Как это работает:</b> Цикл сам распределяет фильтры по минутам. "
            "Чем больше лотов у фильтра — тем чаще он поднимается."
        )

    lines.append("")
    lines.append("Добавь/убери фильтры галочкой:")
    return "\n".join(lines)


@router.callback_query(F.data == "cycles")
async def cb_cycles(call: CallbackQuery, db: Database) -> None:
    await call.answer()
    async with db.session_factory() as session:
        result = await session.execute(select(Cycle).order_by(Cycle.id))
        cycles = result.scalars().all()
    await call.message.edit_text(
        "<b>Циклы</b>\n\nЦикл объединяет несколько фильтров в одно расписание "
        "с авто-распределением по минутам.",
        reply_markup=cycles_menu(cycles),
    )


@router.callback_query(F.data == "cycle_create")
async def cb_cycle_create(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(CycleCreate.waiting_for_name)
    await call.message.edit_text(
        "Введи название цикла (например, «60 робуксов в час»):",
        reply_markup=back_button("cycles"),
    )


@router.message(CycleCreate.waiting_for_name)
async def msg_cycle_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip()[:255])
    await state.set_state(CycleCreate.waiting_for_start)
    await message.answer("Время старта (HH:MM, например 14:00):")


@router.message(CycleCreate.waiting_for_start)
async def msg_cycle_start(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    try:
        h, m = (int(p) for p in text.split(":"))
        assert 0 <= h < 24 and 0 <= m < 60
    except (ValueError, AssertionError):
        await message.answer("Неверный формат. Пример: 14:00")
        return
    await state.update_data(start_time=f"{h:02d}:{m:02d}")
    await state.set_state(CycleCreate.waiting_for_duration)
    await message.answer("Длительность цикла в минутах (например 60):")


@router.message(CycleCreate.waiting_for_duration)
async def msg_cycle_duration(
    message: Message, state: FSMContext, db: Database, bump_engine
) -> None:
    try:
        duration = int(message.text.strip())
        assert duration > 0
    except (ValueError, AssertionError):
        await message.answer("Нужно положительное число.")
        return
    data = await state.get_data()
    async with db.session_factory() as session:
        cycle = Cycle(
            name=data["name"],
            start_time=data["start_time"],
            duration_minutes=duration,
        )
        session.add(cycle)
        await session.commit()
        new_id = cycle.id
    await state.clear()
    await _send_cycle_card(message, db, new_id, bump_engine)


async def _send_cycle_card(message: Message, db: Database, cycle_id: int, bump_engine) -> None:
    async with db.session_factory() as session:
        cycle = await session.get(
            Cycle, cycle_id,
            options=[selectinload(Cycle.filters).selectinload(Filter.lots)],
        )
        if cycle is None:
            return
        result = await session.execute(select(Filter).order_by(Filter.id))
        all_filters = result.scalars().all()
    live_lots = getattr(bump_engine, "_live_lots", [])
    await message.answer(
        _format_cycle(cycle, all_filters, live_lots),
        reply_markup=cycle_card(cycle, all_filters),
    )


async def _open_cycle(call: CallbackQuery, db: Database, cycle_id: int, bump_engine) -> None:
    async with db.session_factory() as session:
        cycle = await session.get(
            Cycle, cycle_id,
            options=[selectinload(Cycle.filters).selectinload(Filter.lots)],
        )
        if cycle is None:
            await call.answer("Цикл не найден", show_alert=True)
            return
        result = await session.execute(select(Filter).order_by(Filter.id))
        all_filters = result.scalars().all()
    live_lots = getattr(bump_engine, "_live_lots", [])
    try:
        await call.message.edit_text(
            _format_cycle(cycle, all_filters, live_lots),
            reply_markup=cycle_card(cycle, all_filters),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("cycle:"))
async def cb_cycle_open(call: CallbackQuery, db: Database, bump_engine) -> None:
    await call.answer()
    cid = int(call.data.split(":")[1])
    await _open_cycle(call, db, cid, bump_engine)


@router.callback_query(F.data.startswith("cycle_toggle:"))
async def cb_cycle_toggle(call: CallbackQuery, db: Database, bump_engine) -> None:
    await call.answer()
    cid = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        cycle = await session.get(Cycle, cid)
        if cycle:
            cycle.enabled = not cycle.enabled
            await session.commit()
    await _open_cycle(call, db, cid, bump_engine)


@router.callback_query(F.data.startswith("cycle_delete:"))
async def cb_cycle_delete(call: CallbackQuery, db: Database) -> None:
    await call.answer("Удалено")
    cid = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        result = await session.execute(
            select(Filter).where(Filter.cycle_id == cid)
        )
        for f in result.scalars():
            f.cycle_id = None
        cycle = await session.get(Cycle, cid)
        if cycle:
            await session.delete(cycle)
            await session.commit()
    await cb_cycles(call, db)


@router.callback_query(F.data.startswith("cycle_start:"))
async def cb_cycle_edit_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    cid = int(call.data.split(":")[1])
    await state.set_state(CycleEditStart.waiting_for_start)
    await state.update_data(cycle_id=cid)
    await call.message.edit_text(
        "Введи новое время старта (HH:MM, например 14:00):",
        reply_markup=back_button(f"cycle:{cid}"),
    )


@router.message(CycleEditStart.waiting_for_start)
async def msg_cycle_edit_start(message: Message, state: FSMContext, db: Database, bump_engine) -> None:
    text = message.text.strip()
    try:
        h, m = (int(p) for p in text.split(":"))
        assert 0 <= h < 24 and 0 <= m < 60
    except (ValueError, AssertionError):
        await message.answer("Неверный формат. Пример: 14:00")
        return
    data = await state.get_data()
    cid = data["cycle_id"]
    async with db.session_factory() as session:
        cycle = await session.get(Cycle, cid)
        if cycle:
            cycle.start_time = f"{h:02d}:{m:02d}"
            await session.commit()
    await state.clear()
    await _send_cycle_card(message, db, cid, bump_engine)


@router.callback_query(F.data.startswith("cycle_duration:"))
async def cb_cycle_edit_duration(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    cid = int(call.data.split(":")[1])
    await state.set_state(CycleEditDuration.waiting_for_minutes)
    await state.update_data(cycle_id=cid)
    await call.message.edit_text(
        "Введи новую длительность цикла в минутах (например 60):",
        reply_markup=back_button(f"cycle:{cid}"),
    )


@router.message(CycleEditDuration.waiting_for_minutes)
async def msg_cycle_edit_duration(message: Message, state: FSMContext, db: Database, bump_engine) -> None:
    try:
        minutes = int(message.text.strip())
        assert minutes > 0
    except (ValueError, AssertionError):
        await message.answer("Нужно положительное число.")
        return
    data = await state.get_data()
    cid = data["cycle_id"]
    async with db.session_factory() as session:
        cycle = await session.get(Cycle, cid)
        if cycle:
            cycle.duration_minutes = minutes
            await session.commit()
    await state.clear()
    await _send_cycle_card(message, db, cid, bump_engine)


@router.callback_query(F.data.startswith("cycle_togglefilter:"))
async def cb_cycle_togglefilter(call: CallbackQuery, db: Database, bump_engine) -> None:
    await call.answer()
    _, cid_s, fid_s = call.data.split(":")
    cid, fid = int(cid_s), int(fid_s)
    async with db.session_factory() as session:
        flt = await session.get(Filter, fid)
        if flt:
            if flt.cycle_id == cid:
                flt.cycle_id = None
            else:
                flt.cycle_id = cid
                last = await session.execute(
                    select(Filter)
                    .where(Filter.cycle_id == cid)
                    .order_by(Filter.order_index.desc())
                )
                last_flt = last.scalars().first()
                flt.order_index = (last_flt.order_index + 1) if last_flt and last_flt.id != flt.id else 0
            await session.commit()
    await _open_cycle(call, db, cid, bump_engine)
