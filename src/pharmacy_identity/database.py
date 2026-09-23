"""SQLite 连接与会话配置。"""

import os
from pathlib import Path

from sqlalchemy import create_engine


def database_url() -> str:
    path = Path(os.getenv("DATABASE_PATH", "data/pharmacy_identity.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def create_database_engine():
    return create_engine(database_url(), connect_args={"check_same_thread": False})
