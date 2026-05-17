"""Persistent registry of authorized users (telegram_id + Playerok credentials)."""
import json
import os
from dataclasses import asdict, dataclass


@dataclass
class UserRecord:
    telegram_id: int
    playerok_cookies: str
    playerok_user_agent: str


class UserRegistryStore:
    """Stores user records in a JSON file. Max users is small (~3), no concurrency."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._users: dict[int, UserRecord] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        for u in data.get("users", []):
            try:
                rec = UserRecord(**u)
                self._users[rec.telegram_id] = rec
            except Exception:
                continue

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        data = {"users": [asdict(u) for u in self._users.values()]}
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def add(self, rec: UserRecord) -> None:
        self._users[rec.telegram_id] = rec
        self._save()

    def remove(self, telegram_id: int) -> bool:
        if telegram_id in self._users:
            del self._users[telegram_id]
            self._save()
            return True
        return False

    def get(self, telegram_id: int) -> UserRecord | None:
        return self._users.get(telegram_id)

    def has(self, telegram_id: int) -> bool:
        return telegram_id in self._users

    def all(self) -> list[UserRecord]:
        return list(self._users.values())
