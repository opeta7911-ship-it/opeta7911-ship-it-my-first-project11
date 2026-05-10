import asyncio
import logging

from database.db import Database
from database.models import Setting
from playerok.client import MyLot, PlayerokClient

logger = logging.getLogger(__name__)

BASE_URL = "https://playerok.com/products/"


class AutoRestoreEngine:
    """Отслеживает продажи лотов и автоматически переопубликовывает их."""

    def __init__(self, db: Database, playerok: PlayerokClient, bot, admin_id: int) -> None:
        self._db = db
        self._playerok = playerok
        self._bot = bot
        self._admin_id = admin_id
        self._task: asyncio.Task | None = None
        # Наполняется при старте: ID лотов, которые уже были проданы ДО запуска бота.
        # Они не восстанавливаются.
        self._known_sold_ids: set[str] = set()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _get_setting(self, key: str, default: str = "") -> str:
        async with self._db.session_factory() as session:
            row = await session.get(Setting, key)
            return row.value if row else default

    async def _is_enabled(self) -> bool:
        return (await self._get_setting("restore_enabled", "0")) == "1"

    async def _get_exclude_keywords(self) -> list[str]:
        raw = await self._get_setting("restore_exclude", "")
        return [k.strip() for k in raw.split(",") if k.strip()]

    async def _run(self) -> None:
        # Снимаем слепок текущих проданных лотов — они не должны восстанавливаться
        try:
            sold = await self._playerok.get_sold_lots()
            self._known_sold_ids = {lot.playerok_id for lot in sold}
            logger.info(
                "AutoRestore: запущен, игнорируем %d ранее проданных лотов",
                len(self._known_sold_ids),
            )
        except Exception:
            logger.exception("AutoRestore: не удалось получить снимок проданных лотов")

        while True:
            await asyncio.sleep(30)
            try:
                if not await self._is_enabled():
                    continue
                await self._check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AutoRestore: ошибка проверки")

    async def _check(self) -> None:
        sold = await self._playerok.get_sold_lots()
        exclude_kws = await self._get_exclude_keywords()

        for lot in sold:
            if lot.playerok_id in self._known_sold_ids:
                continue
            # Новый проданный лот
            self._known_sold_ids.add(lot.playerok_id)

            if any(kw.lower() in lot.name.lower() for kw in exclude_kws):
                logger.info("AutoRestore: лот '%s' исключён по ключевому слову", lot.name)
                continue

            await self._notify_sold(lot)

            try:
                price_rub = (
                    lot.raw_price_kopecks / 100
                    if lot.raw_price_kopecks > 0
                    else lot.price_kopecks / 100
                )
                cost_kopecks = await self._playerok.republish_lot(lot.playerok_id, price_rub)
                logger.info(
                    "AutoRestore: лот '%s' восстановлен (стоимость=%d коп)",
                    lot.name, cost_kopecks,
                )
                await self._notify_restored(lot, cost_kopecks)
            except Exception as exc:
                logger.exception("AutoRestore: не удалось восстановить '%s'", lot.name)
                await self._notify_restore_failed(lot, str(exc))

    async def _send(self, text: str) -> None:
        try:
            await self._bot.send_message(
                self._admin_id, text, disable_web_page_preview=True
            )
        except Exception:
            logger.exception("AutoRestore: не удалось отправить уведомление")

    async def _notify_sold(self, lot: MyLot) -> None:
        url = f"{BASE_URL}{lot.slug}"
        await self._send(
            f"🛒 <b>Продан!</b>\n"
            f'<a href="{url}">{lot.name}</a>\n'
            f"Цена: <b>{lot.price_kopecks / 100:.0f}₽</b>\n"
            f"Восстанавливаю..."
        )

    async def _notify_restored(self, lot: MyLot, cost_kopecks: int) -> None:
        url = f"{BASE_URL}{lot.slug}"
        await self._send(
            f"🔄 <b>Восстановлен!</b>\n"
            f'<a href="{url}">{lot.name}</a>\n'
            f"Цена лота: {lot.price_kopecks / 100:.0f}₽  |  "
            f"Стоимость публикации: {cost_kopecks / 100:.0f}₽"
        )

    async def _notify_restore_failed(self, lot: MyLot, error: str) -> None:
        url = f"{BASE_URL}{lot.slug}"
        await self._send(
            f"❌ <b>Не удалось восстановить!</b>\n"
            f'<a href="{url}">{lot.name}</a>\n'
            f"Ошибка: {error}"
        )
