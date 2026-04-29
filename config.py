import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    telegram_token: str
    admin_id: int
    playerok_cookies: str
    playerok_user_agent: str
    database_url: str
    log_level: str


def load_config() -> Config:
    return Config(
        telegram_token=os.environ["TELEGRAM_TOKEN"],
        admin_id=int(os.environ["ADMIN_ID"]),
        playerok_cookies=os.getenv("PLAYEROK_COOKIES", ""),
        playerok_user_agent=os.getenv(
            "PLAYEROK_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        ),
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
