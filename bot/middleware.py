"""Per-update middleware that authorizes the user and injects their context.

Unregistered users are silently ignored — no reply, as if the bot didn't exist.
"""
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from core.user_context import UserContextRegistry
from database.registry import UserRegistryStore

logger = logging.getLogger(__name__)


class UserContextMiddleware(BaseMiddleware):
    def __init__(
        self,
        contexts: UserContextRegistry,
        store: UserRegistryStore,
        admin_id: int,
    ) -> None:
        self._contexts = contexts
        self._store = store
        self._admin_id = admin_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return
        ctx = self._contexts.get(user.id)
        if ctx is None:
            logger.debug("Ignored event from unregistered user %d", user.id)
            return

        data["db"] = ctx.db
        data["playerok"] = ctx.playerok
        data["bump_engine"] = ctx.bump_engine
        data["user_context"] = ctx
        data["contexts"] = self._contexts
        data["store"] = self._store
        data["admin_id"] = self._admin_id

        return await handler(event, data)
