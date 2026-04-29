from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from bot.keyboards.menus import main_menu

router = Router()


def _greeting(running: bool) -> str:
    status = "🟢 ВКЛ" if running else "🔴 ВЫКЛ"
    return (
        "<b>Playerok Auto-Bump</b>\n\n"
        f"Автоподнятие: {status}\n\n"
        "Управление: фильтры → циклы → запуск."
    )


@router.message(CommandStart())
async def cmd_start(message: Message, bump_engine) -> None:
    await message.answer(
        _greeting(bump_engine.enabled),
        reply_markup=main_menu(bump_engine.enabled),
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    from bot.handlers.stats import show_stats_menu
    await show_stats_menu(message)


@router.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery, bump_engine) -> None:
    await call.message.edit_text(
        _greeting(bump_engine.enabled),
        reply_markup=main_menu(bump_engine.enabled),
    )
    await call.answer()


@router.callback_query(F.data == "toggle_engine")
async def cb_toggle_engine(call: CallbackQuery, bump_engine) -> None:
    if bump_engine.enabled:
        await bump_engine.stop()
    else:
        await bump_engine.start()
    await call.message.edit_text(
        _greeting(bump_engine.enabled),
        reply_markup=main_menu(bump_engine.enabled),
    )
    await call.answer("Готово")


@router.callback_query(F.data == "settings")
async def cb_settings(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "<b>Настройки</b>\n\n"
        "Cookies и user-agent Playerok задаются через .env (PLAYEROK_COOKIES, "
        "PLAYEROK_USER_AGENT). Перезапусти бота после изменения.",
        reply_markup=main_menu_back(),
    )
    await call.answer()


def main_menu_back():
    from bot.keyboards.menus import back_button
    return back_button()
