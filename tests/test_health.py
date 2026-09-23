"""基础存活检查。"""

from sqlalchemy import create_engine

from pharmacy_identity import create_app


def test_health_uses_sqlite() -> None:
    app = create_app(create_engine("sqlite:///:memory:"))
    response = app.test_client().get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "storage": "sqlite"}
