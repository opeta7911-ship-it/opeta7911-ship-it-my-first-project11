from aiogram import Dispatcher

from bot.handlers import cycles, filters, main, restore, stats


def register_all(dp: Dispatcher) -> None:
    dp.include_router(main.router)
    dp.include_router(restore.router)
    dp.include_router(filters.router)
    dp.include_router(cycles.router)
    dp.include_router(stats.router)
