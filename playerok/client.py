import asyncio
import re
from dataclasses import dataclass

from playerokapi.account import Account


PLAYEROK_URL_RE = re.compile(r"playerok\.com/products/([^/?#\s]+)", re.IGNORECASE)


def extract_slug(url: str) -> str | None:
    m = PLAYEROK_URL_RE.search(url.strip())
    return m.group(1) if m else None


@dataclass
class LotInfo:
    playerok_id: str
    slug: str
    name: str
    price_kopecks: int
    bump_cost_kopecks: int
    bump_priority_status_id: str


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

    async def get_lot_by_url(self, url: str) -> LotInfo:
        slug = extract_slug(url)
        if not slug:
            raise ValueError("Невалидная ссылка Playerok")

        async with self._lock:
            account = await self._ensure_account()
            item = await asyncio.to_thread(account.get_item, None, slug)
            statuses = await asyncio.to_thread(
                account.get_item_priority_statuses, item.id, item.price
            )

        cheapest = min(statuses, key=lambda s: s.price)
        return LotInfo(
            playerok_id=item.id,
            slug=slug,
            name=item.name,
            price_kopecks=int(item.price * 100),
            bump_cost_kopecks=int(cheapest.price * 100),
            bump_priority_status_id=cheapest.id,
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
                account.publish_item, playerok_id, priority_status_id
            )

    async def is_item_active(self, playerok_id: str) -> bool:
        try:
            async with self._lock:
                account = await self._ensure_account()
                item = await asyncio.to_thread(account.get_item, playerok_id, None)
            status = getattr(item, "status", None)
            if status is None:
                return True
            status_str = str(status).upper()
            return "APPROVED" in status_str or "ACTIVE" in status_str
        except Exception:
            return False
