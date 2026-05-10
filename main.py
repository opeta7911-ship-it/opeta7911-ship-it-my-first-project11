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
from scheduler.restore import AutoRestoreEngine

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
        # Короткий идентификатор из URL — чтобы видеть что поднимаются РАЗНЫЕ лоты
        # (даже если у нескольких лотов одинаковое название на Playerok)
        slug_id = lot.url.rsplit("/", 1)[-1].split("-", 1)[0][:8]
        if success:
            text = (
                f'🚀 <a href="{lot.url}">{lot.name}</a>\n'
                f"Поднято ✅  ({cost_kopecks / 100:.0f}₽ / {lot.price_kopecks / 100:.0f}₽)\n"
                f"<code>#{lot.id} · {slug_id}</code>"
            )
        else:
            text = (
                f'❌ <a href="{lot.url}">{lot.name}</a>\n'
                f"Не поднят — {error or 'неизвестная ошибка'}\n"
                f"<code>#{lot.id} · {slug_id}</code>"
            )
        try:
            await bot.send_message(config.admin_id, text, disable_web_page_preview=True)
        except Exception:
            logger.exception("Failed to send bump notification")

    engine = BumpEngine(db=db, playerok=playerok, on_result=on_bump_result)
    restore_engine = AutoRestoreEngine(
        db=db, playerok=playerok, bot=bot, admin_id=config.admin_id
    )

    dp = Dispatcher()
    dp["db"] = db
    dp["playerok"] = playerok
    dp["bump_engine"] = engine
    dp["config"] = config

    register_all(dp)

    logger.info("Bot starting...")
    await restore_engine.start()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await engine.stop()
        await restore_engine.stop()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
