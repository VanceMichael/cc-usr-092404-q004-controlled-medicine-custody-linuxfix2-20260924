"""Flask HTTP 边界：鉴权、幂等重放、事务与路由装配。

约定：
- 所有写操作在单个数据库事务内完成；领域错误回滚并映射状态码；
- ``X-Actor-Id`` 标识经办人，角色字段视图在服务层裁剪；
- 写请求可携带 ``Idempotency-Key``，重放同一键返回首次响应；
- 离线扫码以报文中的 ``occurred_at``（真实发生时间）入链。
"""

import hashlib
import json

from flask import Flask, jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import directory, ledger, transfers, views
from .database import create_database_engine
from .errors import DomainError, ManualReviewRequired
from .schema import (
    actors,
    idempotent_requests,
    transfer_lines,
    transfer_requests,
)
from . import clock


def create_app(engine=None) -> Flask:
    app = Flask(__name__)
    storage = engine or create_database_engine()

    # --- 横切：鉴权、事务、错误映射 ------------------------------------------

    def current_actor(required: bool = True):
        actor_id = request.headers.get("X-Actor-Id")
        if not actor_id:
            if required:
                raise DomainError("UNAUTHENTICATED", "缺少 X-Actor-Id", 401)
            return None
        with storage.connect() as conn:
            row = conn.execute(
                select(actors).where(actors.c.actor_id == actor_id)
            ).mappings().first()
        if not row or not row["active"]:
            raise DomainError("UNAUTHENTICATED", f"经办人 {actor_id} 不存在或已停用", 401)
        return dict(row)

    def require_role(actor, *roles: str):
        if actor["role"] not in roles:
            raise DomainError("FORBIDDEN", f"需要角色 {roles}", 403)

    def payload() -> dict:
        data = request.get_json(silent=True)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise DomainError("VALIDATION_FAILED", "请求体必须是 JSON 对象", 422)
        return data

    def fingerprint() -> str:
        raw = f"{request.method} {request.path}\n{request.get_data(as_text=True)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def run_service(fn, *args, commit_on_manual: bool = False, **kwargs):
        """开启事务执行领域函数，统一提交/回滚与错误映射。"""
        body = payload()
        idem_key = request.headers.get("Idempotency-Key")
        with storage.begin() as conn:
            if idem_key:
                stored = conn.execute(
                    select(idempotent_requests).where(
                        idempotent_requests.c.idempotency_key == idem_key
                    )
                ).mappings().first()
                if stored:
                    if stored["request_fingerprint"] != fingerprint():
                        raise DomainError("IDEMPOTENCY_CONFLICT", "幂等键对应不同请求内容", 422)
                    return (
                        jsonify(json.loads(stored["response_body"])),
                        stored["response_status"],
                    )
            try:
                result = fn(conn, body, *args, **kwargs)
                status = 200
            except ManualReviewRequired as exc:
                # 扫码事实与人工复核单必须落库：不抛出，事务随 with 正常提交。
                if not commit_on_manual:
                    raise
                result = {
                    "code": exc.code,
                    "message": exc.message,
                    "manual_review_id": exc.review_id,
                }
                status = 202
            except IntegrityError as exc:
                raise DomainError("CONFLICT", f"唯一性/约束冲突：{exc.orig}", 409) from exc
            if idem_key:
                conn.execute(
                    idempotent_requests.insert().values(
                        idempotency_key=idem_key,
                        request_fingerprint=fingerprint(),
                        response_status=status,
                        response_body=json.dumps(result, ensure_ascii=False),
                        created_at=clock.now(),
                    )
                )
            return jsonify(result), status

    @app.errorhandler(DomainError)
    def _domain_error(exc: DomainError):
        return jsonify(code=exc.code, message=exc.message), exc.status_code

    @app.cli.command("bootstrap-admin")
    def bootstrap_admin() -> None:
        """创建首个 ADMIN 经办人（仅当库中尚无任何经办人时可用）。"""
        import click

        actor_id = click.prompt("actor_id", default="admin")
        display_name = click.prompt("display_name", default="系统管理员")
        with storage.begin() as conn:
            exists = conn.execute(select(actors.c.actor_id).limit(1)).first()
            if exists:
                click.echo("库中已存在经办人，拒绝自举写入。")
                return
            directory.register_actor(conn, actor_id, display_name, "ADMIN")
        click.echo(f"管理员 {actor_id} 已创建。")

    @app.get("/health")
    def health():
        with storage.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        return jsonify(status="ok", storage="sqlite")

    # --- 管理端：主体/许可/关系/药品基线 --------------------------------------

    @app.post("/admin/parties")
    def admin_party():
        actor = current_actor()
        require_role(actor, "ADMIN")
        body = payload()

        def op(conn, _body):
            directory.register_party(
                conn, body["code"], body["name"], body["type"], body["jurisdiction"]
            )
            return {"code": body["code"], "registered": True}

        return run_service(op)

    @app.post("/admin/actors")
    def admin_actor():
        actor = current_actor()
        require_role(actor, "ADMIN")
        body = payload()

        def op(conn, _body):
            directory.register_actor(
                conn, body["actor_id"], body["display_name"], body["role"],
                body.get("party_code"), bool(body.get("can_review", False)),
            )
            return {"actor_id": body["actor_id"], "registered": True}

        return run_service(op)

    @app.post("/admin/quarantines")
    def admin_quarantine():
        actor = current_actor()
        require_role(actor, "ADMIN")
        body = payload()

        def op(conn, _body):
            directory.register_quarantine(conn, body["code"], body["name"], body["jurisdiction"])
            return {"code": body["code"], "registered": True}

        return run_service(op)

    @app.post("/admin/drugs")
    def admin_drug():
        actor = current_actor()
        require_role(actor, "ADMIN")
        body = payload()

        def op(conn, _body):
            directory.register_drug(conn, body["product_code"], body["name"], body["controlled_class"])
            return {"product_code": body["product_code"], "registered": True}

        return run_service(op)

    @app.post("/admin/batches")
    def admin_batch():
        actor = current_actor()
        require_role(actor, "ADMIN")
        body = payload()

        def op(conn, _body):
            directory.register_batch(
                conn, body["product_code"], body["batch_number"],
                int(body["initial_qty"]), body["holder_party_code"], body.get("expiry_date"),
            )
            return {
                "product_code": body["product_code"],
                "batch_number": body["batch_number"],
                "registered": True,
            }

        return run_service(op)

    @app.post("/admin/licenses")
    def admin_license():
        actor = current_actor()
        require_role(actor, "ADMIN")
        body = payload()

        def op(conn, _body):
            license_id = directory.add_license(
                conn, body["party_code"], body["license_type"], body["license_number"],
                body["valid_from"], body["valid_to"],
            )
            return {"license_id": license_id, "registered": True}

        return run_service(op)

    @app.post("/admin/licenses/<int:license_id>/status")
    def admin_license_status(license_id: int):
        actor = current_actor()
        require_role(actor, "ADMIN")
        body = payload()

        def op(conn, _body):
            directory.change_license_status(
                conn, license_id, body["status"], body.get("note", ""), actor["actor_id"]
            )
            return {"license_id": license_id, "status": body["status"]}

        return run_service(op)

    @app.post("/admin/licenses/<int:license_id>/correct")
    def admin_license_correct(license_id: int):
        actor = current_actor()
        require_role(actor, "ADMIN")
        body = payload()

        def op(conn, _body):
            new_id = directory.correct_license(
                conn, license_id,
                license_number=body["license_number"],
                valid_from=body["valid_from"],
                valid_to=body["valid_to"],
                actor_id=actor["actor_id"],
                note=body.get("note", ""),
            )
            return {"old_license_id": license_id, "new_license_id": new_id}

        return run_service(op)

    @app.post("/admin/relationships")
    def admin_relationship():
        actor = current_actor()
        require_role(actor, "ADMIN")
        body = payload()

        def op(conn, _body):
            rel_id = directory.add_relationship(
                conn, body["party_code"], body["related_party_code"], body["relation_type"],
                body["valid_from"], body["valid_to"],
            )
            return {"relationship_id": rel_id, "registered": True}

        return run_service(op)

    @app.post("/admin/relationships/<int:relationship_id>/correct")
    def admin_relationship_correct(relationship_id: int):
        actor = current_actor()
        require_role(actor, "ADMIN")
        body = payload()

        def op(conn, _body):
            new_id = directory.correct_relationship(
                conn, relationship_id,
                related_party_code=body["related_party_code"],
                valid_from=body["valid_from"],
                valid_to=body["valid_to"],
                actor_id=actor["actor_id"],
                note=body.get("note", ""),
            )
            return {"old_relationship_id": relationship_id, "new_relationship_id": new_id}

        return run_service(op)

    # --- 调拨工作流 -----------------------------------------------------------

    @app.post("/transfers")
    def create_transfer():
        actor = current_actor()

        def op(conn, body):
            return transfers.create_request(conn, body, actor["actor_id"])

        return run_service(op)

    @app.get("/transfers")
    def list_transfers():
        actor = current_actor()
        with storage.connect() as conn:
            return jsonify(views.list_requests(conn, actor))

    @app.get("/transfers/<number>")
    def get_transfer(number: str):
        actor = current_actor()
        with storage.begin() as conn:
            req = transfers._request(conn, number)
            lines = transfers._lines(conn, req["id"])
            return jsonify(views.project_request(conn, actor, req, lines))

    @app.get("/transfers/<number>/chain")
    def get_chain(number: str):
        actor = current_actor()
        with storage.begin() as conn:
            req = transfers._request(conn, number)
            # 非审计角色也必须在该申请履职范围内，投影同时完成字段裁剪。
            views.project_request(conn, actor, req, transfers._lines(conn, req["id"]))
            return jsonify(views.project_chain(conn, actor, req["id"]))

    @app.post("/transfers/<number>/reviews")
    def review_transfer(number: str):
        actor = current_actor()

        def op(conn, body):
            return transfers.add_review(conn, number, body, actor["actor_id"])

        return run_service(op)

    @app.post("/transfers/<number>/dispatch")
    def dispatch_transfer(number: str):
        actor = current_actor()

        def op(conn, body):
            return transfers.dispatch(conn, number, body, actor["actor_id"])

        return run_service(op)

    @app.post("/transfers/<number>/scans")
    def scan_transfer(number: str):
        # 车载/手持设备可能无人值守登录：X-Actor-Id 可选。
        actor = current_actor(required=False)

        def op(conn, body):
            return transfers.record_scan(
                conn, number, body, actor["actor_id"] if actor else None
            )

        return run_service(op, commit_on_manual=True)

    @app.post("/transfers/<number>/receive")
    def receive_transfer(number: str):
        actor = current_actor()

        def op(conn, body):
            return transfers.receive(conn, number, body, actor["actor_id"])

        return run_service(op)

    @app.post("/transfers/<number>/cancel")
    def cancel_transfer(number: str):
        actor = current_actor()

        def op(conn, body):
            return transfers.cancel(conn, number, body, actor["actor_id"])

        return run_service(op)

    @app.post("/transfers/<number>/reject")
    def reject_transfer(number: str):
        actor = current_actor()

        def op(conn, body):
            return transfers.reject(conn, number, body, actor["actor_id"])

        return run_service(op)

    @app.post("/transfers/<number>/return")
    def return_transfer(number: str):
        actor = current_actor()
        body0 = payload()
        leg = body0.get("leg", "complete")

        def op(conn, body):
            if leg == "pickup":
                return transfers.return_start(conn, number, body, actor["actor_id"])
            return transfers.return_complete(conn, number, body, actor["actor_id"])

        return run_service(op)

    @app.post("/investigations/<case_number>/resolve")
    def resolve_case(case_number: str):
        actor = current_actor()

        def op(conn, body):
            return transfers.resolve_investigation(conn, case_number, body, actor["actor_id"])

        return run_service(op)

    @app.post("/manual-reviews/<int:review_id>/resolve")
    def resolve_manual(review_id: int):
        actor = current_actor()

        def op(conn, body):
            return transfers.resolve_manual_review(conn, review_id, body, actor["actor_id"])

        return run_service(op)

    @app.post("/system/timeout-sweep")
    def timeout_sweep():
        actor = current_actor()
        require_role(actor, "ADMIN")

        def op(conn, body):
            return transfers.run_timeout_sweep(conn, body.get("at"))

        return run_service(op)

    # --- 查询与审计 -----------------------------------------------------------

    @app.get("/batches/<product_code>/<batch_number>/location")
    def batch_location(product_code: str, batch_number: str):
        actor = current_actor()
        at = request.args.get("at")
        with storage.connect() as conn:
            holders = ledger.locate_batch(conn, product_code, batch_number, at)
            allowed = actor["role"] in ("ADMIN", "AUDITOR")
            if not allowed:
                party = actor["party_code"]
                if any(h["holder_code"] == party for h in holders):
                    allowed = True
                elif actor["role"] == "STORE_STAFF":
                    # 门店可追踪本店收发过的批号（在途时也要知道在谁手里）。
                    from sqlalchemy import or_

                    involved = conn.execute(
                        select(transfer_requests.c.id)
                        .select_from(
                            transfer_requests.join(
                                transfer_lines,
                                transfer_lines.c.request_id == transfer_requests.c.id,
                            )
                        )
                        .where(transfer_lines.c.product_code == product_code)
                        .where(transfer_lines.c.batch_number == batch_number)
                        .where(
                            or_(
                                transfer_requests.c.sender_code == party,
                                transfer_requests.c.receiver_code == party,
                            )
                        )
                        .limit(1)
                    ).first()
                    allowed = involved is not None
            if not allowed:
                raise DomainError("FORBIDDEN", "该批号不在你的履职范围内", 403)
            return jsonify(
                product_code=product_code,
                batch_number=batch_number,
                as_of=at or "now",
                holders=holders,
            )

    @app.get("/transfers/<number>/custody-intervals")
    def custody_intervals(number: str):
        actor = current_actor()
        require_role(actor, "ADMIN", "AUDITOR")
        with storage.connect() as conn:
            req = transfers._request(conn, number)
            return jsonify(
                request_number=number,
                intervals=ledger.holder_intervals(conn, req["id"]),
            )

    @app.get("/audit/conservation")
    def audit_conservation():
        actor = current_actor()
        require_role(actor, "ADMIN", "AUDITOR")
        with storage.connect() as conn:
            return jsonify(ledger.verify_conservation(conn))

    @app.get("/audit/hash-chain")
    def audit_hash_chain():
        actor = current_actor()
        require_role(actor, "ADMIN", "AUDITOR")
        with storage.connect() as conn:
            return jsonify(ledger.verify_hash_chain(conn))

    return app
