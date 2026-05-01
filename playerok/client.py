import asyncio
import re
from dataclasses import dataclass

from playerokapi.account import Account
from playerokapi.enums import ItemStatuses


PLAYEROK_URL_RE = re.compile(r"playerok\.com/products/([^/?#\s]+)", re.IGNORECASE)
BASE_URL = "https://playerok.com/products/"


def extract_slug(url: str) -> str | None:
    m = PLAYEROK_URL_RE.search(url.strip())
    return m.group(1) if m else None


@dataclass
class MyLot:
    playerok_id: str
    slug: str
    name: str
    price_kopecks: int
    bump_cost_kopecks: int
    bump_priority_status_id: str

    @property
    def url(self) -> str:
        return f"{BASE_URL}{self.slug}"


class PlayerokClient:
    """Async-обёртка над синхронным PlayerokAPI."""

    def __init__(self, cookies: str, user_agent: str) -> None:
        self._cookies = cookies
        self._user_agent = user_agent
        self._account: Account | None = None
        self._lock = asyncio.Lock()

    async def _ensure_account(self) -> Account:
        if self._account is None:
            self._account = await asyncio.to_thread(self._init_account)
        return self._account

    def _init_account(self) -> Account:
        return Account(cookies=self._cookies, user_agent=self._user_agent).get()

    def update_credentials(self, cookies: str, user_agent: str) -> None:
        self._cookies = cookies
        self._user_agent = user_agent
        self._account = None

    def _fetch_all_my_lots_sync(self, account: Account) -> list:
        """Загружает все активные лоты аккаунта с пагинацией."""
        user = account.get_user(id=account.id)
        all_items = []
        cursor = None
        while True:
            page = user.get_items(
                count=24,
                statuses=[ItemStatuses.APPROVED],
                after_cursor=cursor,
            )
            all_items.extend(page.items)
            if not page.page_info.end_cursor or len(page.items) < 24:
                break
            cursor = page.page_info.end_cursor
        return all_items

    async def get_my_lots(self) -> list[MyLot]:
        """Возвращает список активных лотов аккаунта (без запроса цены поднятия)."""
        async with self._lock:
            account = await self._ensure_account()
            raw_items = await asyncio.to_thread(self._fetch_all_my_lots_sync, account)

        return [
            MyLot(
                playerok_id=item.id,
                slug=item.slug,
                name=item.name,
                price_kopecks=int(item.price * 100),
                bump_cost_kopecks=0,
                bump_priority_status_id="",
            )
            for item in raw_items
        ]

    async def get_lot_bump_cost(self, playerok_id: str, price_rub: float) -> tuple[int, str]:
        """Запрашивает актуальную стоимость поднятия для конкретного лота."""
        async with self._lock:
            account = await self._ensure_account()
            statuses = await asyncio.to_thread(
                account.get_item_priority_statuses, playerok_id, price_rub
            )
        cheapest = min(statuses, key=lambda s: s.price)
        return int(cheapest.price * 100), cheapest.id
        return await asyncio.to_thread(
            account.get_item_priority_statuses, item_id, price
        )

    async def refresh_bump_cost(self, playerok_id: str, price_rub: float) -> tuple[int, str]:
        async with self._lock:
            account = await self._ensure_account()
            statuses = await asyncio.to_thread(
                account.get_item_priority_statuses, playerok_id, price_rub
            )
        cheapest = min(statuses, key=lambda s: s.price)
        return int(cheapest.price * 100), cheapest.id

    async def bump(self, playerok_id: str, priority_status_id: str) -> None:
        async with self._lock:
            account = await self._ensure_account()
            await asyncio.to_thread(
                account.increase_item_priority_status, playerok_id, priority_status_id
            )

    async def is_item_active(self, playerok_id: str) -> bool:
        try:
            async with self._lock:
                account = await self._ensure_account()
                item = await asyncio.to_thread(account.get_item, playerok_id, None)
            status = getattr(item, "status", None)
            if status is None:
                return True
            return "APPROVED" in str(status).upper() or "ACTIVE" in str(status).upper()
        except Exception:
            return False
