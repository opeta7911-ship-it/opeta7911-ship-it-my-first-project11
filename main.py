import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.handlers import register_all
from bot.middleware import UserContextMiddleware
from config import load_config
from core.user_context import UserContextRegistry
from database.registry import UserRecord, UserRegistryStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = "data"
LEGACY_DB = "bot.db"


def _migrate_legacy_db(admin_id: int) -> None:
    """Move pre-multiuser bot.db to data/bot_<admin_id>.db if needed."""
    target = f"{DATA_DIR}/bot_{admin_id}.db"
    if os.path.exists(LEGACY_DB) and not os.path.exists(target):
        os.makedirs(DATA_DIR, exist_ok=True)
        os.rename(LEGACY_DB, target)
        logger.info("Migrated %s → %s", LEGACY_DB, target)


async def main() -> None:
    config = load_config()
    os.makedirs(DATA_DIR, exist_ok=True)
    _migrate_legacy_db(config.admin_id)

    store = UserRegistryStore(f"{DATA_DIR}/users.json")
    if not store.has(config.admin_id):
        store.add(
            UserRecord(
                telegram_id=config.admin_id,
                playerok_cookies=config.playerok_cookies,
                playerok_user_agent=config.playerok_user_agent,
            )
        )
        logger.info("Admin %d added to user registry", config.admin_id)

    bot = Bot(
        token=config.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    contexts = UserContextRegistry(DATA_DIR, bot)
    for rec in store.all():
        try:
            await contexts.create_and_start(rec)
            logger.info("Started runtime for user %d", rec.telegram_id)
        except Exception:
            logger.exception("Failed to start runtime for user %d", rec.telegram_id)

    dp = Dispatcher()
    middleware = UserContextMiddleware(contexts, store, config.admin_id)
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)

    register_all(dp)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="🚀 Старт"),
            BotCommand(command="adduser", description="➕ Добавить юзера (admin)"),
            BotCommand(command="removeuser", description="➖ Удалить юзера (admin)"),
            BotCommand(command="listusers", description="📋 Список юзеров (admin)"),
        ]
    )

    logger.info("Bot starting...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await contexts.stop_all()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
