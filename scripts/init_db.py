"""Create the database and all tables. Safe to re-run.

    python -m scripts.init_db          # create if missing
    python -m scripts.init_db --reset  # drop and recreate the tables
"""

import sys

from sqlalchemy import create_engine, text

from app.config import get_settings
from app.db import Base, engine
from app import models  # noqa: F401  -- registers the tables on Base.metadata


def create_database_if_missing() -> None:
    settings = get_settings()
    server = create_engine(settings.server_url, future=True)
    with server.connect() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{settings.mysql_database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )
        conn.commit()
    server.dispose()
    print(f"database ready: {settings.mysql_database}")


def main() -> None:
    reset = "--reset" in sys.argv
    create_database_if_missing()
    if reset:
        Base.metadata.drop_all(bind=engine)
        print("dropped existing tables")
    Base.metadata.create_all(bind=engine)
    print("tables ready:", ", ".join(sorted(Base.metadata.tables)))


if __name__ == "__main__":
    main()
