import os
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str
    database_url: str
    application_id: int | None = None
    test_guild_id: int | None = None

    @classmethod
    def load(cls, *, database_only=False):
        load_dotenv()
        required = ["DATABASE_URL"] if database_only else ["DATABASE_URL", "DISCORD_TOKEN"]
        missing = [key for key in required if not os.getenv(key, "").strip()]
        if missing:
            raise ValueError("Set these variables in .env: " + ", ".join(missing))
        host = urlparse(os.environ["DATABASE_URL"]).hostname or ""
        if "-pooler." in host:
            raise ValueError(
                "Use the direct/unpooled Neon DATABASE_URL; session locking requires it."
            )
        return cls(
            os.getenv("DISCORD_TOKEN", ""),
            os.environ["DATABASE_URL"],
            int(os.environ["DISCORD_APPLICATION_ID"])
            if os.getenv("DISCORD_APPLICATION_ID")
            else None,
            int(os.environ["TEST_GUILD_ID"]) if os.getenv("TEST_GUILD_ID") else None,
        )
