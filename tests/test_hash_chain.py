"""哈希链：任何对历史节点的改动或删除都会被审计发现。"""

from sqlalchemy import text

from helpers import approve, clean_receive, conservation, create_transfer, dispatch


def _full_chain(client):
    number = create_transfer(client, qty=6)
    approve(client, number)
    dispatch(client, number, seal="SEAL-H")
    assert clean_receive(client, number, qty=6, seal="SEAL-H").status_code == 200
    return number


def test_hash_chain_valid_initially(client, seed):
    _full_chain(client)
    resp = client.get("/audit/hash-chain", headers={"X-Actor-Id": "aud1"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["event_count"] >= 5


def test_hash_chain_detects_content_tampering(client, seed, app):
    _full_chain(client)
    # 绕过 ORM 直接改库（模拟 DBA 篡改）：事件触发器禁止 UPDATE，
    # 临时摘掉触发器，证明哈希链是第二道防线。
    eng = app.extensions["test_engine"]
    with eng.begin() as conn:
        conn.execute(text("DROP TRIGGER trg_custody_events_no_update"))
        conn.execute(text("UPDATE custody_events SET note='forged' WHERE event_type='DISPATCHED'"))
    resp = client.get("/audit/hash-chain", headers={"X-Actor-Id": "aud1"})
    assert resp.get_json()["ok"] is False
    assert any("哈希不匹配" in p for p in resp.get_json()["problems"])


def test_hash_chain_detects_deleted_event(client, seed, app):
    _full_chain(client)
    eng = app.extensions["test_engine"]
    with eng.begin() as conn:
        conn.execute(text("DROP TRIGGER trg_custody_events_no_delete"))
        conn.execute(text(
            "DELETE FROM custody_events WHERE id = "
            "(SELECT id FROM custody_events WHERE event_type='REVIEWED' ORDER BY id LIMIT 1)"
        ))
    resp = client.get("/audit/hash-chain", headers={"X-Actor-Id": "aud1"})
    body = resp.get_json()
    assert body["ok"] is False
    assert any("前驱哈希断裂" in p for p in body["problems"])


def test_audit_reports_every_moment_unique_holder_and_conservation(client, seed):
    number = create_transfer(client, qty=6)
    approve(client, number)
    dispatch(client, number, seal="SEAL-AUD")
    assert clean_receive(client, number, qty=6, seal="SEAL-AUD").status_code == 200
    report = conservation(client)
    assert report["ok"]
    for batch in report["batches"]:
        # 同一批号在非 LOSS 保管人之间不重复计数（唯一保管归属）。
        active = [h for h in batch["holders"] if h["holder_type"] != "LOSS"]
        # 各保管人持有量之和等于在账总量，且不存在同一单位双归属。
        assert sum(h["qty"] for h in active) == batch["active_qty"]
