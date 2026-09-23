"""测试夹具：共享内存 SQLite，表由 ORM 元数据创建。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from pharmacy_identity import create_app
from pharmacy_identity.triggers import create_schema


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def service(engine):
    from pharmacy_identity.service import CustodyService

    return CustodyService(engine)


@pytest.fixture()
def client(engine):
    return create_app(engine).test_client()


@pytest.fixture()
def world(service):
    """搭好两个门店、一个承运、一个批号与期初库存。"""
    s1 = service.register_store("S1", "城北店", "LIC-S1")
    s2 = service.register_store("S2", "城南店", "LIC-S2")
    c1 = service.register_carrier("C1", "安通配送", "QA-C1", "active", "2031-01-01T00:00:00+00:00")
    service.register_staff("u1", s1["id"], "pharmacist")
    service.register_staff("u2", s1["id"], "pharmacist")
    service.register_staff("u3", s2["id"], "pharmacist")
    service.register_batch("B-001", "芬太尼贴剂")
    service.genesis_stock("B-001", s1["id"], 100, "admin")
    return {"s1": s1["id"], "s2": s2["id"], "c1": c1["id"]}


def happy_path(service, world, request_no="TR-1", qty=10, seal="SEAL-1"):
    service.create_request(
        request_no, "B-001", qty, world["s1"], world["s2"], world["c1"],
        "u1", "2026-09-23T10:00:00+00:00", "2030-09-23T14:00:00+00:00",
    )
    service.review_request(request_no, "u2", "approve")
    service.review_request(request_no, "u3", "approve")
    service.release_request(request_no, seal, "u1", "T-A")
    service.confirm_seal(request_no, seal, "u3", "T-B")
    return service.receive(request_no, qty, "u3", "T-B")
