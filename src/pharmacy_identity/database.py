"""SQLite 连接与会话配置。"""

import os
from pathlib import Path

from sqlalchemy import create_engine, event


def database_url() -> str:
    path = Path(os.getenv("DATABASE_PATH", "data/pharmacy_identity.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def create_database_engine(url: str | None = None):
    engine = create_engine(
        url or database_url(),
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        # 多终端/多进程并发确认时由数据库裁决；WAL 允许读写并发。
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine
