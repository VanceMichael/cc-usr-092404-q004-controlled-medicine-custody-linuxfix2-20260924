"""Alembic 迁移必须能在空库上一路升到 head 并回滚。"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def _alembic_config(db_path: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_migrations_upgrade_and_downgrade(tmp_path):
    db = tmp_path / "migrated.sqlite3"
    cfg = _alembic_config(str(db))
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db}")
    names = set(inspect(engine).get_table_names())
    for expected in (
        "parties", "licenses", "relationships", "drugs", "batches",
        "transfer_requests", "transfer_lines", "reviews",
        "custody_events", "custody_items", "investigations",
        "risk_notes", "manual_reviews", "idempotent_requests",
    ):
        assert expected in names, expected

    with engine.connect() as conn:
        triggers = {
            r[0]
            for r in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ))
        }
    assert "trg_custody_events_no_update" in triggers
    assert "trg_reviews_no_delete" in triggers

    # 回滚两级后全部业务表消失。
    command.downgrade(cfg, "001_foundation")
    names = set(inspect(engine).get_table_names())
    assert "custody_events" not in names
    assert "service_metadata" in names
