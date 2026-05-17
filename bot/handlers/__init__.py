from aiogram import Dispatcher

from bot.handlers import cycles, filters, main, restore, stats, users


def register_all(dp: Dispatcher) -> None:
    # users router first so /adduser, /removeuser, /listusers, /cancel
    # match before any generic handlers.
    dp.include_router(users.router)
    dp.include_router(main.router)
    dp.include_router(restore.router)
    dp.include_router(filters.router)
    dp.include_router(cycles.router)
    dp.include_router(stats.router)
