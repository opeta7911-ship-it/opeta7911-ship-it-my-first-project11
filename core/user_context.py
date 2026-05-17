"""Per-user runtime container: DB, Playerok client, BumpEngine, AutoRestoreEngine."""
import logging

from aiogram import Bot

from database.db import Database
from database.models import Lot
from database.registry import UserRecord
from playerok.client import PlayerokClient
from scheduler.bumper import BumpEngine
from scheduler.restore import AutoRestoreEngine

logger = logging.getLogger(__name__)


class UserContext:
    """All runtime objects belonging to a single user."""

    def __init__(
        self,
        telegram_id: int,
        db: Database,
        playerok: PlayerokClient,
        bump_engine: BumpEngine,
        restore_engine: AutoRestoreEngine,
    ) -> None:
        self.telegram_id = telegram_id
        self.db = db
        self.playerok = playerok
        self.bump_engine = bump_engine
        self.restore_engine = restore_engine

    async def stop(self) -> None:
        try:
            await self.bump_engine.stop()
        except Exception:
            logger.exception("user=%d bump_engine.stop failed", self.telegram_id)
        try:
            await self.restore_engine.stop()
        except Exception:
            logger.exception("user=%d restore_engine.stop failed", self.telegram_id)
        try:
            await self.db.close()
        except Exception:
            logger.exception("user=%d db.close failed", self.telegram_id)


class UserContextRegistry:
    """Manages live UserContext instances. Each user has independent DB and engines."""

    def __init__(self, data_dir: str, bot: Bot) -> None:
        self._data_dir = data_dir
        self._bot = bot
        self._contexts: dict[int, UserContext] = {}

    def get(self, telegram_id: int) -> UserContext | None:
        return self._contexts.get(telegram_id)

    def all(self) -> list[UserContext]:
        return list(self._contexts.values())

    def db_path(self, telegram_id: int) -> str:
        return f"{self._data_dir}/bot_{telegram_id}.db"

    async def create_and_start(self, rec: UserRecord) -> UserContext:
        db_url = f"sqlite+aiosqlite:///{self.db_path(rec.telegram_id)}"
        db = Database(db_url)
        await db.init()
        playerok = PlayerokClient(rec.playerok_cookies, rec.playerok_user_agent)

        bot = self._bot
        user_id = rec.telegram_id

        async def on_bump_result(
            lot: Lot, success: bool, cost_kopecks: int, error: str | None
        ) -> None:
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
                await bot.send_message(user_id, text, disable_web_page_preview=True)
            except Exception:
                logger.exception("user=%d failed to send bump notification", user_id)

        bump_engine = BumpEngine(db=db, playerok=playerok, on_result=on_bump_result)
        # AutoRestoreEngine sends notifications to the user; its admin_id param
        # is just the chat to notify, so pass the user's own telegram_id.
        restore_engine = AutoRestoreEngine(
            db=db, playerok=playerok, bot=bot, admin_id=rec.telegram_id
        )
        await restore_engine.start()

        ctx = UserContext(rec.telegram_id, db, playerok, bump_engine, restore_engine)
        self._contexts[rec.telegram_id] = ctx
        return ctx

    async def stop_and_remove(self, telegram_id: int) -> bool:
        ctx = self._contexts.pop(telegram_id, None)
        if ctx is None:
            return False
        await ctx.stop()
        return True

    async def stop_all(self) -> None:
        for ctx in list(self._contexts.values()):
            await ctx.stop()
        self._contexts.clear()
