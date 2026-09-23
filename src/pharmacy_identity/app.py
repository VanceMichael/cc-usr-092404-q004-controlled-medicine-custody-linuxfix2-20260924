"""Flask HTTP 边界。

角色字段裁剪在 GET /requests/<no>?role=store|carrier|audit 完成；
写接口只做参数解包，所有不变量由 CustodyService 保证。
"""

from flask import Flask, jsonify, request

from sqlalchemy import text

from .database import create_database_engine
from .service import CustodyService, DomainError


def create_app(engine=None) -> Flask:
    app = Flask(__name__)
    storage = engine or create_database_engine()
    service = CustodyService(storage)

    @app.errorhandler(DomainError)
    def _domain_error(error: DomainError):
        return jsonify(error={"code": error.code, "message": str(error)}), error.status

    def body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise DomainError("bad_json", "请求体必须是 JSON 对象", 400)
        return data

    def require(data: dict, *fields: str):
        missing = [f for f in fields if data.get(f) is None]
        if missing:
            raise DomainError("missing_fields", f"缺少字段：{','.join(missing)}", 400)
        return [data[f] for f in fields]

    @app.get("/health")
    def health():
        with storage.connect() as connection:
            connection.execute(text("SELECT 1"))
        return jsonify(status="ok", storage="sqlite")

    # ------------------------------------------------------------ 登记管理

    @app.post("/admin/stores")
    def register_store():
        data = body()
        code, name, license_no = require(data, "code", "name", "license_no")
        return jsonify(service.register_store(code, name, license_no, data.get("license_status", "active"))), 201

    @app.post("/admin/stores/<int:store_id>/license")
    def update_store_license(store_id: int):
        data = body()
        license_status, = require(data, "license_status")
        return jsonify(service.update_store_license(store_id, license_status, data.get("license_no")))

    @app.post("/admin/staff")
    def register_staff():
        data = body()
        user_code, store_id, role = require(data, "user_code", "store_id", "role")
        return jsonify(service.register_staff(user_code, store_id, role)), 201

    @app.post("/admin/carriers")
    def register_carrier():
        data = body()
        code, name, qualification_no = require(data, "code", "name", "qualification_no")
        return jsonify(
            service.register_carrier(
                code,
                name,
                qualification_no,
                data.get("qualification_status", "active"),
                data.get("qualified_until"),
            )
        ), 201

    @app.post("/admin/carriers/<int:carrier_id>/qualification")
    def update_carrier_qualification(carrier_id: int):
        data = body()
        qualification_status, = require(data, "qualification_status")
        return jsonify(
            service.update_carrier_qualification(carrier_id, qualification_status, data.get("qualified_until"))
        )

    @app.post("/admin/batches")
    def register_batch():
        data = body()
        batch_no, drug_name = require(data, "batch_no", "drug_name")
        return jsonify(service.register_batch(batch_no, drug_name)), 201

    @app.post("/admin/genesis")
    def genesis_stock():
        data = body()
        batch_no, store_id, quantity, actor = require(data, "batch_no", "store_id", "quantity", "actor")
        return jsonify(service.genesis_stock(batch_no, store_id, int(quantity), actor)), 201

    @app.post("/admin/revalidate")
    def revalidate():
        data = body()
        author, = require(data, "author")
        return jsonify(
            service.revalidate_after_correction(
                author, data.get("store_id"), data.get("carrier_id")
            )
        )

    # ------------------------------------------------------------ 调拨流程

    @app.post("/requests")
    def create_request():
        data = body()
        require(
            data,
            "request_no", "batch_no", "quantity", "from_store_id", "to_store_id",
            "carrier_id", "created_by", "planned_at", "expected_by",
        )
        return (
            jsonify(
                service.create_request(
                    data["request_no"],
                    data["batch_no"],
                    int(data["quantity"]),
                    int(data["from_store_id"]),
                    int(data["to_store_id"]),
                    int(data["carrier_id"]),
                    data["created_by"],
                    data["planned_at"],
                    data["expected_by"],
                )
            ),
            201,
        )

    @app.get("/requests/<request_no>")
    def get_request(request_no: str):
        role = request.args.get("role")
        if not role:
            raise DomainError("missing_role", "必须以 ?role=store|carrier|audit 指定查看角色", 400)
        return jsonify(service.request_view_for_role(request_no, role))

    @app.get("/requests/<request_no>/chain")
    def request_chain(request_no: str):
        # 仅审计角色可取完整事件链
        if request.args.get("role") != "audit":
            raise DomainError("forbidden", "完整事件链仅审计角色可取", 403)
        return jsonify(service.event_chain(request_no))

    @app.post("/requests/<request_no>/reviews")
    def review_request(request_no: str):
        data = body()
        reviewer, decision = require(data, "reviewer", "decision")
        return jsonify(service.review_request(request_no, reviewer, decision, data.get("comment", "")))

    @app.post("/requests/<request_no>/release")
    def release_request(request_no: str):
        data = body()
        seal_no, actor, terminal_id = require(data, "seal_no", "actor", "terminal_id")
        return jsonify(
            service.release_request(
                request_no,
                seal_no,
                actor,
                terminal_id,
                data.get("occurred_at"),
                bool(data.get("offline", False)),
            )
        )

    @app.post("/requests/<request_no>/seal")
    def confirm_seal(request_no: str):
        data = body()
        seal_no, actor, terminal_id = require(data, "seal_no", "actor", "terminal_id")
        return jsonify(
            service.confirm_seal(
                request_no,
                seal_no,
                actor,
                terminal_id,
                data.get("occurred_at"),
                bool(data.get("offline", False)),
            )
        )

    @app.post("/requests/<request_no>/receive")
    def receive(request_no: str):
        data = body()
        actual_quantity, actor, terminal_id = require(data, "actual_quantity", "actor", "terminal_id")
        return jsonify(
            service.receive(
                request_no,
                int(actual_quantity),
                actor,
                terminal_id,
                bool(data.get("damage_reported", False)),
                data.get("occurred_at"),
                bool(data.get("offline", False)),
            )
        )

    @app.post("/requests/<request_no>/cancel")
    def cancel(request_no: str):
        data = body()
        actor, reason = require(data, "actor", "reason")
        return jsonify(service.cancel_request(request_no, actor, reason))

    @app.post("/requests/<request_no>/reject")
    def reject_delivery(request_no: str):
        data = body()
        actor, reason = require(data, "actor", "reason")
        return jsonify(service.reject_delivery(request_no, actor, reason))

    @app.post("/requests/<request_no>/return")
    def return_goods(request_no: str):
        data = body()
        actor, reason = require(data, "actor", "reason")
        return jsonify(service.return_goods(request_no, actor, reason, data.get("quantity")))

    # ------------------------------------------------------------ 扫描/恢复/调查

    @app.post("/scans/offline")
    def offline_scan():
        data = body()
        request_no, scan_type, terminal_id, occurred_at = require(
            data, "request_no", "scan_type", "terminal_id", "occurred_at"
        )
        return jsonify(
            service.offline_scan(
                request_no, scan_type, terminal_id, occurred_at, data.get("seal_no")
            )
        )

    @app.post("/system/recover")
    def recover():
        return jsonify(service.run_due_tasks())

    @app.post("/investigations/<int:investigation_id>/resolve")
    def resolve_investigation(investigation_id: int):
        data = body()
        action, actor, note = require(data, "action", "actor", "note")
        return jsonify(
            service.resolve_investigation(
                investigation_id, action, actor, note, data.get("quantity")
            )
        )

    # ------------------------------------------------------------ 查询与审计

    @app.get("/batches/<batch_no>/location")
    def locate_batch(batch_no: str):
        return jsonify(service.locate_batch(batch_no, request.args.get("at")))

    @app.get("/audit")
    def audit():
        if request.args.get("role") != "audit":
            raise DomainError("forbidden", "审计结论仅审计角色可取", 403)
        return jsonify(service.audit(request.args.get("at")))

    return app
