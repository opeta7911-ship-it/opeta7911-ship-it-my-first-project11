import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.handlers import register_all
from config import load_config
from database.db import Database
from database.models import Lot
from playerok.client import PlayerokClient
from scheduler.bumper import BumpEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_config()

    db = Database(config.database_url)
    await db.init()

    playerok = PlayerokClient(config.playerok_cookies, config.playerok_user_agent)

    bot = Bot(
        token=config.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    async def on_bump_result(lot: Lot, success: bool, cost_kopecks: int, error: str | None) -> None:
        if success:
            text = (
                f'🚀 <a href="{lot.url}">{lot.name}</a>\n'
                f"Поднято ✅  ({cost_kopecks / 100:.0f}₽ / {lot.price_kopecks / 100:.0f}₽)"
            )
        else:
            text = (
                f'❌ <a href="{lot.url}">{lot.name}</a>\n'
                f"Не поднят — {error or 'неизвестная ошибка'}"
            )
        try:
            await bot.send_message(config.admin_id, text, disable_web_page_preview=True)
        except Exception:
            logger.exception("Failed to send bump notification")

    engine = BumpEngine(db=db, playerok=playerok, on_result=on_bump_result)

    dp = Dispatcher()
    dp["db"] = db
    dp["playerok"] = playerok
    dp["bump_engine"] = engine
    dp["config"] = config

    register_all(dp)

    logger.info("Bot starting...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await engine.stop()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
