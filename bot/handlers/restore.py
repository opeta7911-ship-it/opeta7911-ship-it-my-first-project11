from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.states import RestoreAddExclude
from database.db import Database
from database.models import Setting

router = Router()


def _restore_menu(enabled: bool, exclude_kws: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    label = "🟢 Авто восстановление: ВКЛ" if enabled else "🔴 Авто восстановление: ВЫКЛ"
    kb.button(text=label, callback_data="restore_toggle")
    excl_label = f"🚫 Не выставлять ({len(exclude_kws)})" if exclude_kws else "🚫 Не выставлять"
    kb.button(text=excl_label, callback_data="restore_exclude_list")
    kb.button(text="◀️ Назад", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()


def _exclude_list_menu(kws: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for kw in kws:
        kb.button(text=f"❌ {kw}", callback_data=f"restore_del_exclude:{kw}")
    kb.button(text="➕ Добавить", callback_data="restore_add_exclude")
    kb.button(text="◀️ Назад", callback_data="restore")
    kb.adjust(1)
    return kb.as_markup()


async def _get_setting(db: Database, key: str, default: str = "") -> str:
    async with db.session_factory() as session:
        row = await session.get(Setting, key)
        return row.value if row else default


async def _set_setting(db: Database, key: str, value: str) -> None:
    async with db.session_factory() as session:
        row = await session.get(Setting, key)
        if row:
            row.value = value
        else:
            session.add(Setting(key=key, value=value))
        await session.commit()


async def _get_state(db: Database) -> tuple[bool, list[str]]:
    enabled = (await _get_setting(db, "restore_enabled", "0")) == "1"
    raw = await _get_setting(db, "restore_exclude", "")
    kws = [k.strip() for k in raw.split(",") if k.strip()]
    return enabled, kws


def _page_text(enabled: bool) -> str:
    status = "🟢 ВКЛ" if enabled else "🔴 ВЫКЛ"
    return (
        f"<b>🔁 Авто восстановление</b>  {status}\n\n"
        "Бот отслеживает покупки твоих лотов каждые 30 секунд.\n"
        "Как только лот продан — приходит уведомление о продаже, "
        "и лот сразу переопубликовывается.\n\n"
        "Лоты которые уже были в «Завершённых» до запуска бота — не трогаются."
    )


@router.callback_query(F.data == "restore")
async def cb_restore(call: CallbackQuery, db: Database) -> None:
    await call.answer()
    enabled, kws = await _get_state(db)
    await call.message.edit_text(_page_text(enabled), reply_markup=_restore_menu(enabled, kws))


@router.callback_query(F.data == "restore_toggle")
async def cb_restore_toggle(call: CallbackQuery, db: Database) -> None:
    enabled, kws = await _get_state(db)
    new_enabled = not enabled
    await _set_setting(db, "restore_enabled", "1" if new_enabled else "0")
    await call.answer("🟢 Включено" if new_enabled else "🔴 Выключено")
    await call.message.edit_text(
        _page_text(new_enabled), reply_markup=_restore_menu(new_enabled, kws)
    )


@router.callback_query(F.data == "restore_exclude_list")
async def cb_restore_exclude_list(call: CallbackQuery, db: Database) -> None:
    await call.answer()
    _, kws = await _get_state(db)
    text = "<b>🚫 Не выставлять</b>\n\nЛоты с этими словами в названии не будут восстанавливаться автоматически."
    if kws:
        text += "\n\n" + "\n".join(f"• <code>{kw}</code>" for kw in kws)
    await call.message.edit_text(text, reply_markup=_exclude_list_menu(kws))


@router.callback_query(F.data == "restore_add_exclude")
async def cb_restore_add_exclude(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(RestoreAddExclude.waiting_for_keyword)
    await call.message.edit_text(
        "✏️ <b>Введи ключевое слово</b>\n\n"
        "Лоты, в названии которых оно встречается, не будут восстанавливаться.\n\n"
        "Например: <code>6б+</code>  или  <code>2000</code>"
    )


@router.message(RestoreAddExclude.waiting_for_keyword)
async def msg_restore_add_exclude(message: Message, state: FSMContext, db: Database) -> None:
    kw = (message.text or "").strip()
    await state.clear()
    if not kw:
        await message.answer("⚠️ Пустое слово, попробуй ещё раз.")
        return
    raw = await _get_setting(db, "restore_exclude", "")
    existing = [k.strip() for k in raw.split(",") if k.strip()]
    if kw not in existing:
        existing.append(kw)
    await _set_setting(db, "restore_exclude", ",".join(existing))
    _, kws = await _get_state(db)
    text = "<b>🚫 Не выставлять</b>\n\nЛоты с этими словами в названии не будут восстанавливаться автоматически."
    if kws:
        text += "\n\n" + "\n".join(f"• <code>{kw}</code>" for kw in kws)
    await message.answer(f"✅ Добавлено: <code>{kw}</code>", reply_markup=_exclude_list_menu(kws))


@router.callback_query(F.data.startswith("restore_del_exclude:"))
async def cb_restore_del_exclude(call: CallbackQuery, db: Database) -> None:
    kw = call.data.split(":", 1)[1]
    raw = await _get_setting(db, "restore_exclude", "")
    existing = [k.strip() for k in raw.split(",") if k.strip() and k.strip() != kw]
    await _set_setting(db, "restore_exclude", ",".join(existing))
    await call.answer(f"Удалено: {kw}")
    _, kws = await _get_state(db)
    text = "<b>🚫 Не выставлять</b>\n\nЛоты с этими словами в названии не будут восстанавливаться автоматически."
    if kws:
        text += "\n\n" + "\n".join(f"• <code>{kw}</code>" for kw in kws)
    await call.message.edit_text(text, reply_markup=_exclude_list_menu(kws))
