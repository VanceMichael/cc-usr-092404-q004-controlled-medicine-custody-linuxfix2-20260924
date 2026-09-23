"""测试夹具：内存 SQLite + 完整建表/触发器 + 一套标准主数据。"""

import pytest
from sqlalchemy import create_engine, text

from pharmacy_identity import create_app
from pharmacy_identity.schema import append_only_triggers, metadata


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        metadata.create_all(conn)
        for ddl in append_only_triggers():
            conn.execute(text(ddl))
    yield eng
    eng.dispose()


@pytest.fixture()
def app(engine):
    application = create_app(engine)
    application.extensions["test_engine"] = engine
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def seed(app):
    """标准主数据：许可窗口足够宽，时间为带时区 UTC 绝对时刻。"""
    from pharmacy_identity import directory

    eng = app.extensions["test_engine"]
    with eng.begin() as conn:
        directory.register_party(conn, "S1", "城南门店", "STORE", "BJ")
        directory.register_party(conn, "S2", "城北门店", "STORE", "BJ")
        directory.register_party(conn, "C1", "城际承运", "CARRIER", "BJ")
        directory.register_quarantine(conn, "Q1", "市级隔离库", "BJ")
        directory.register_actor(conn, "admin", "管理员", "ADMIN")
        directory.register_actor(conn, "s1mgr", "城南店长", "STORE_STAFF", "S1", can_review=True)
        directory.register_actor(conn, "s2mgr", "城北店长", "STORE_STAFF", "S2", can_review=True)
        directory.register_actor(conn, "s1staff", "城南店员", "STORE_STAFF", "S1")
        directory.register_actor(conn, "s2staff", "城北店员", "STORE_STAFF", "S2")
        directory.register_actor(conn, "car1", "承运司机", "CARRIER_STAFF", "C1")
        directory.register_actor(conn, "aud1", "合规审计", "AUDITOR")
        directory.register_drug(conn, "D1", "盐酸哌甲酯片", "第一类")
        directory.register_batch(conn, "D1", "B2026-01", 100, "S1", expiry_date="2027-01-01")
        directory.register_batch(conn, "D1", "B2026-02", 20, "S1", expiry_date="2027-06-01")
        for party, ltype, number in (
            ("S1", "STORE_CONTROLLED_DRUG", "LIC-S1"),
            ("S2", "STORE_CONTROLLED_DRUG", "LIC-S2"),
            ("C1", "CARRIER_QUALIFICATION", "LIC-C1"),
        ):
            directory.add_license(
                conn, party, ltype, number,
                "2020-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00",
            )
        directory.add_relationship(
            conn, "S1", "C1", "STORE_CARRIER",
            "2020-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00",
        )
        directory.add_relationship(
            conn, "S1", "S2", "SENDER_RECEIVER",
            "2020-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00",
        )

    class Ids:
        admin = "admin"
        s1mgr = "s1mgr"
        s2mgr = "s2mgr"
        s1staff = "s1staff"
        s2staff = "s2staff"
        carrier = "car1"
        auditor = "aud1"

    return Ids()
