"""Admin-only commands to manage authorized users.

Only the admin (whose Telegram ID is set in .env as ADMIN_ID) can use these.
For everyone else these commands are silently ignored by the middleware.
"""
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.states import AddUser
from core.user_context import UserContextRegistry
from database.registry import UserRecord, UserRegistryStore

logger = logging.getLogger(__name__)
router = Router()

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


def _is_admin(message: Message, admin_id: int) -> bool:
    return message.from_user is not None and message.from_user.id == admin_id


# --- Command handlers (registered before state handlers so commands always win) ---


@router.message(Command("adduser"))
async def cmd_adduser(message: Message, admin_id: int, state: FSMContext) -> None:
    if not _is_admin(message, admin_id):
        return
    await state.set_state(AddUser.waiting_for_telegram_id)
    await message.answer(
        "Введи Telegram ID нового пользователя (одно число).\n"
        "Чтобы отменить — /cancel"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, admin_id: int, state: FSMContext) -> None:
    if not _is_admin(message, admin_id):
        return
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Отменено.")


@router.message(Command("removeuser"))
async def cmd_removeuser(
    message: Message,
    admin_id: int,
    store: UserRegistryStore,
    contexts: UserContextRegistry,
) -> None:
    if not _is_admin(message, admin_id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Использование: <code>/removeuser TELEGRAM_ID</code>")
        return
    try:
        tg_id = int(parts[1])
    except ValueError:
        await message.answer("Неверный ID.")
        return
    if tg_id == admin_id:
        await message.answer("Себя удалить нельзя.")
        return
    if not store.has(tg_id):
        await message.answer(f"Пользователь <code>{tg_id}</code> не найден.")
        return
    await contexts.stop_and_remove(tg_id)
    store.remove(tg_id)
    await message.answer(
        f"✅ Пользователь <code>{tg_id}</code> удалён.\n"
        f"Его база данных сохранена на диске (на случай если захочешь вернуть)."
    )


@router.message(Command("listusers"))
async def cmd_listusers(
    message: Message,
    admin_id: int,
    store: UserRegistryStore,
    contexts: UserContextRegistry,
) -> None:
    if not _is_admin(message, admin_id):
        return
    users = store.all()
    if not users:
        await message.answer("Пользователей нет.")
        return
    lines = ["<b>Пользователи:</b>"]
    for u in users:
        running = "🟢" if contexts.get(u.telegram_id) else "🔴"
        tag = " <b>(admin)</b>" if u.telegram_id == admin_id else ""
        lines.append(f"{running} <code>{u.telegram_id}</code>{tag}")
    await message.answer("\n".join(lines))


# --- State handlers (must be after command handlers) ---


@router.message(AddUser.waiting_for_telegram_id)
async def adduser_id(
    message: Message,
    admin_id: int,
    state: FSMContext,
    store: UserRegistryStore,
) -> None:
    if not _is_admin(message, admin_id):
        return
    text = (message.text or "").strip()
    try:
        tg_id = int(text)
    except ValueError:
        await message.answer("Это не число. Попробуй ещё раз или /cancel.")
        return
    if store.has(tg_id):
        await message.answer(f"Пользователь <code>{tg_id}</code> уже добавлен.")
        await state.clear()
        return
    await state.update_data(telegram_id=tg_id)
    await state.set_state(AddUser.waiting_for_cookies)
    await message.answer(
        "Теперь пришли cookies Playerok этого пользователя одной строкой.\n"
        "Если не знаешь как — посмотри в .env твоего бота (PLAYEROK_COOKIES).\n"
        "/cancel — отмена."
    )


@router.message(AddUser.waiting_for_cookies)
async def adduser_cookies(
    message: Message,
    admin_id: int,
    state: FSMContext,
    store: UserRegistryStore,
    contexts: UserContextRegistry,
) -> None:
    if not _is_admin(message, admin_id):
        return
    cookies = (message.text or "").strip()
    if not cookies:
        await message.answer("Cookies пустые. Попробуй ещё раз или /cancel.")
        return
    data = await state.get_data()
    tg_id = int(data["telegram_id"])
    rec = UserRecord(
        telegram_id=tg_id,
        playerok_cookies=cookies,
        playerok_user_agent=DEFAULT_USER_AGENT,
    )
    store.add(rec)
    try:
        await contexts.create_and_start(rec)
        await message.answer(
            f"✅ Пользователь <code>{tg_id}</code> добавлен и запущен.\n"
            f"Скажи ему написать боту /start"
        )
    except Exception as exc:
        logger.exception("Failed to start context for user %d", tg_id)
        await message.answer(
            f"⚠️ Пользователь <code>{tg_id}</code> сохранён, но движок не стартанул: "
            f"<code>{exc}</code>\nВозможно cookies неверные."
        )
    await state.clear()
