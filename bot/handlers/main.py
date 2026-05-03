from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete as sql_delete

from bot.keyboards.menus import main_menu, reset_confirm_menu
from database.db import Database
from database.models import BumpHistory, Cycle, Filter, Lot

router = Router()


def _greeting(running: bool) -> str:
    status = "🟢 ВКЛ" if running else "🔴 ВЫКЛ"
    return (
        "<b>Playerok Auto-Bump</b>\n\n"
        f"Автоподнятие: {status}"
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
    await call.answer()
    await call.message.edit_text(
        _greeting(bump_engine.enabled),
        reply_markup=main_menu(bump_engine.enabled),
    )


@router.callback_query(F.data == "toggle_engine")
async def cb_toggle_engine(call: CallbackQuery, bump_engine) -> None:
    if bump_engine.enabled:
        await bump_engine.stop()
    else:
        await bump_engine.start()
    try:
        await call.answer()
    except Exception:
        pass
    try:
        await call.message.edit_text(
            _greeting(bump_engine.enabled),
            reply_markup=main_menu(bump_engine.enabled),
        )
    except Exception:
        pass


@router.callback_query(F.data == "settings")
async def cb_settings(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(
        "<b>Настройки</b>\n\n"
        "Cookies и user-agent Playerok задаются через .env (PLAYEROK_COOKIES, "
        "PLAYEROK_USER_AGENT). Перезапусти бота после изменения.",
        reply_markup=main_menu_back(),
    )


def main_menu_back():
    from bot.keyboards.menus import back_button
    return back_button()


@router.callback_query(F.data == "reset_request")
async def cb_reset_request(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_text(
        "⚠️ <b>Сброс всех данных</b>\n\n"
        "Будут удалены все фильтры, циклы, лоты и история поднятий.\n"
        "Настройки подключения (.env) останутся.\n\n"
        "Это действие <b>необратимо</b>. Продолжить?",
        reply_markup=reset_confirm_menu(),
    )


@router.callback_query(F.data == "reset_confirm")
async def cb_reset_confirm(call: CallbackQuery, db: Database, bump_engine) -> None:
    await call.answer()
    await bump_engine.stop()
    async with db.session_factory() as session:
        await session.execute(sql_delete(BumpHistory))
        await session.execute(sql_delete(Lot))
        await session.execute(sql_delete(Filter))
        await session.execute(sql_delete(Cycle))
        await session.commit()
    await call.message.edit_text(
        "✅ Все данные сброшены. Бот остановлен.\n\nНажми /start чтобы начать заново.",
        reply_markup=main_menu(bump_engine.enabled),
    )
