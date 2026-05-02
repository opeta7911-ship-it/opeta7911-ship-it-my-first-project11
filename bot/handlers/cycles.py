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


def _format_cycle(cycle: Cycle, all_filters: list[Filter]) -> str:
    in_cycle = [f for f in cycle.filters]
    total_lots = sum(len(f.lots) for f in in_cycle)
    capacity = cycle.duration_minutes
    fit = "✅" if total_lots <= capacity else "⚠️"
    lines = [
        f"<b>Цикл: {cycle.name}</b>",
        "",
        f"Состояние: {'🟢 ВКЛ' if cycle.enabled else '🔴 ВЫКЛ'}",
        f"Старт: {cycle.start_time}",
        f"Длительность: {cycle.duration_minutes} мин",
        f"Лотов в цикле: {total_lots} {fit}",
    ]
    if total_lots > capacity:
        lines.append(
            f"⚠️ Лотов больше чем минут. Увеличь длительность до {total_lots} или убери лоты."
        )
    lines.append("")
    lines.append("Отметь фильтры галочкой чтобы добавить/убрать из цикла:")
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
    message: Message, state: FSMContext, db: Database
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
    await _send_cycle_card(message, db, new_id)


async def _send_cycle_card(message: Message, db: Database, cycle_id: int) -> None:
    async with db.session_factory() as session:
        cycle = await session.get(
            Cycle, cycle_id,
            options=[selectinload(Cycle.filters).selectinload(Filter.lots)],
        )
        if cycle is None:
            return
        result = await session.execute(
            select(Filter).options(selectinload(Filter.lots)).order_by(Filter.id)
        )
        all_filters = result.scalars().all()
    await message.answer(
        _format_cycle(cycle, all_filters),
        reply_markup=cycle_card(cycle, all_filters),
    )


async def _open_cycle(call: CallbackQuery, db: Database, cycle_id: int) -> None:
    async with db.session_factory() as session:
        cycle = await session.get(
            Cycle,
            cycle_id,
            options=[selectinload(Cycle.filters).selectinload(Filter.lots)],
        )
        if cycle is None:
            await call.answer("Цикл не найден", show_alert=True)
            return
        result = await session.execute(
            select(Filter).options(selectinload(Filter.lots)).order_by(Filter.id)
        )
        all_filters = result.scalars().all()
    try:
        await call.message.edit_text(
            _format_cycle(cycle, all_filters),
            reply_markup=cycle_card(cycle, all_filters),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("cycle:"))
async def cb_cycle_open(call: CallbackQuery, db: Database) -> None:
    await call.answer()
    cid = int(call.data.split(":")[1])
    await _open_cycle(call, db, cid)


@router.callback_query(F.data.startswith("cycle_toggle:"))
async def cb_cycle_toggle(call: CallbackQuery, db: Database) -> None:
    await call.answer()
    cid = int(call.data.split(":")[1])
    async with db.session_factory() as session:
        cycle = await session.get(Cycle, cid)
        if cycle:
            cycle.enabled = not cycle.enabled
            await session.commit()
    await _open_cycle(call, db, cid)


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
async def msg_cycle_edit_start(message: Message, state: FSMContext, db: Database) -> None:
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
    await _send_cycle_card(message, db, cid)


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
async def msg_cycle_edit_duration(message: Message, state: FSMContext, db: Database) -> None:
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
    await _send_cycle_card(message, db, cid)


@router.callback_query(F.data.startswith("cycle_togglefilter:"))
async def cb_cycle_togglefilter(call: CallbackQuery, db: Database) -> None:
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
    await _open_cycle(call, db, cid)
