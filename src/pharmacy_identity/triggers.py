"""只追加事件链的触发器 DDL（迁移与测试共用）。

custody_events 是审计事实：纠正只能以新事件表达，任何 UPDATE/DELETE 直接被库拒绝。
"""

CUSTODY_APPEND_ONLY_TRIGGERS = [
    """
    CREATE TRIGGER trg_custody_no_update
    BEFORE UPDATE ON custody_events
    BEGIN
        SELECT RAISE(ABORT, 'custody_events is append-only: updates are forbidden');
    END;
    """,
    """
    CREATE TRIGGER trg_custody_no_delete
    BEFORE DELETE ON custody_events
    BEGIN
        SELECT RAISE(ABORT, 'custody_events is append-only: deletes are forbidden');
    END;
    """,
]


def install_custody_triggers(dbapi_connection) -> None:
    cursor = dbapi_connection.cursor()
    for ddl in CUSTODY_APPEND_ONLY_TRIGGERS:
        cursor.execute(ddl)
    cursor.close()


def create_schema(engine) -> None:
    """测试辅助：按 ORM 元数据建表并安装只追加触发器（生产由 Alembic 迁移负责）。"""
    from .models import metadata

    metadata.create_all(engine)
    with engine.begin() as connection:
        for ddl in CUSTODY_APPEND_ONLY_TRIGGERS:
            connection.exec_driver_sql(ddl)
