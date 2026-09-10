"""Environment-backed configuration. Secrets never live in source control."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_database: str = "indiashipments"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # "real" calls Gemini. "stub" replaces only the model with canned
    # replies for testing; the tools and database stay real.
    agent_mode: str = "real"

    pin_api_base_url: str = "https://api.postalpincode.in"
    pin_api_timeout_seconds: float = 3.0

    def _url(self, database: str | None) -> URL:
        """Build the connection URL via SQLAlchemy so that passwords containing
        '@', ':' or '/' are escaped correctly rather than breaking the URL."""
        return URL.create(
            "mysql+pymysql",
            username=self.mysql_user,
            password=self.mysql_password,
            host=self.mysql_host,
            port=self.mysql_port,
            database=database,
            query={"charset": "utf8mb4"},
        )

    @property
    def database_url(self) -> URL:
        """Connection URL including the database name."""
        return self._url(self.mysql_database)

    @property
    def server_url(self) -> URL:
        """Connection URL without a database, for CREATE DATABASE."""
        return self._url(None)


@lru_cache
def get_settings() -> Settings:
    return Settings()
