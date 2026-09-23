"""SQLite 连接与会话配置。

多进程 gunicorn 部署下写事务用 BEGIN IMMEDIATE 立即取写锁串行化，
配合各唯一约束保证双人复核、封签单次确认等并发不变量。
"""

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
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - 驱动回调
        # 关闭驱动隐式事务，改由 begin 事件显式开启 BEGIN IMMEDIATE
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _begin_immediate(connection):
        # 所有事务立即升级为写锁，消除 deferred 事务下的写冲突死锁/忙错。
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine
