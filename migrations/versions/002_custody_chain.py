"""受控药品调拨保管链路。

只追加事件链：库存与保管归属一律由 custody_events 重放得到，
任何历史节点都不提供 UPDATE/DELETE 路径；纠正通过新事件（重验、风险说明、补偿）表达。
"""

from alembic import op
import sqlalchemy as sa

revision = "002_custody_chain"
down_revision = "001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stores",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String, nullable=False, unique=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("license_no", sa.String, nullable=False),
        sa.Column("license_status", sa.String, nullable=False),
        sa.Column("updated_at", sa.String, nullable=False),
    )
    op.create_table(
        "staff",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_code", sa.String, nullable=False, unique=True),
        sa.Column("store_id", sa.Integer, sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("active", sa.Integer, nullable=False, server_default="1"),
    )
    op.create_table(
        "carriers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String, nullable=False, unique=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("qualification_no", sa.String, nullable=False),
        sa.Column("qualification_status", sa.String, nullable=False),
        sa.Column("qualified_until", sa.String, nullable=True),
        sa.Column("updated_at", sa.String, nullable=False),
    )
    op.create_table(
        "drug_batches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("batch_no", sa.String, nullable=False, unique=True),
        sa.Column("drug_name", sa.String, nullable=False),
        sa.Column("controlled", sa.Integer, nullable=False, server_default="1"),
    )
    op.create_table(
        "transfer_requests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_no", sa.String, nullable=False, unique=True),
        sa.Column("batch_no", sa.String, nullable=False),
        sa.Column("drug_name", sa.String, nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("from_store_id", sa.Integer, sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("to_store_id", sa.Integer, sa.ForeignKey("stores.id"), nullable=False),
        sa.Column("carrier_id", sa.Integer, sa.ForeignKey("carriers.id"), nullable=False),
        sa.Column("planned_at", sa.String, nullable=False),
        sa.Column("expected_by", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        # 计划时刻冻结的门店许可、承运资质快照（JSON 字符串）
        sa.Column("frozen_snapshot", sa.String, nullable=False),
        sa.Column("seal_no", sa.String, nullable=True),
        sa.Column("received_qty", sa.Integer, nullable=True),
        sa.Column("requires_manual_review", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by", sa.String, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("released_at", sa.String, nullable=True),
        sa.Column("completed_at", sa.String, nullable=True),
    )
    op.create_table(
        "request_reviews",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("transfer_requests.id"), nullable=False),
        sa.Column("reviewer_user_code", sa.String, nullable=False),
        sa.Column("decision", sa.String, nullable=False),
        sa.Column("comment", sa.String, nullable=True),
        sa.Column("created_at", sa.String, nullable=False),
        sa.UniqueConstraint("request_id", "reviewer_user_code", name="uq_review_one_per_reviewer"),
    )
    op.create_table(
        "custody_events",
        sa.Column("id", sa.Integer, primary_key=True),
        # 期初库存事件的 request_id 为空
        sa.Column("request_id", sa.Integer, sa.ForeignKey("transfer_requests.id"), nullable=True),
        sa.Column("batch_no", sa.String, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column(
            "event_type",
            sa.String,
            nullable=False,
        ),  # genesis|created|review|released|seal_confirmed|received|quarantined|compensated|cancelled|risk_note|manual_review|scan|license_revalidated
        # 保管移交的两端，格式 "<type>:<id>"，type ∈ store|carrier|quarantine
        sa.Column("from_custodian", sa.String, nullable=True),
        sa.Column("to_custodian", sa.String, nullable=True),
        sa.Column("quantity", sa.Integer, nullable=False, server_default="0"),
        # 真实发生时间（离线扫描可为过去）与服务器落库时间分别保存
        sa.Column("occurred_at", sa.String, nullable=False),
        sa.Column("recorded_at", sa.String, nullable=False),
        sa.Column("actor", sa.String, nullable=False),
        sa.Column("terminal_id", sa.String, nullable=True),
        sa.Column("source", sa.String, nullable=False, server_default="online"),  # online|offline|recovery
        sa.Column("idempotency_key", sa.String, nullable=True),
        sa.Column("payload", sa.String, nullable=False, server_default="{}"),
    )
    op.create_index("ix_custody_batch_time", "custody_events", ["batch_no", "occurred_at"])
    op.create_index("ix_custody_request_seq", "custody_events", ["request_id", "seq"])
    # 出库/接收对同一申请只能发生一次：并发双提交由数据库拒绝
    op.create_index(
        "ix_custody_singleton_released",
        "custody_events",
        ["request_id", "event_type"],
        unique=True,
        sqlite_where=sa.text("event_type = 'released'"),
    )
    op.create_index(
        "ix_custody_singleton_received",
        "custody_events",
        ["request_id", "event_type"],
        unique=True,
        sqlite_where=sa.text("event_type = 'received'"),
    )
    # 幂等键全局唯一：重复扫码必须返回原结果
    op.create_index(
        "ix_custody_idempotency",
        "custody_events",
        ["idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "seal_confirmations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("transfer_requests.id"), nullable=False),
        sa.Column("seal_no", sa.String, nullable=False),
        sa.Column("terminal_id", sa.String, nullable=False),
        sa.Column("confirmed_by", sa.String, nullable=False),
        sa.Column("occurred_at", sa.String, nullable=False),
        sa.Column("recorded_at", sa.String, nullable=False),
        # 同一封签只能被确认一次：两个终端抢确认时第二个落库失败
        sa.UniqueConstraint("request_id", "seal_no", name="uq_seal_confirmed_once"),
    )
    op.create_table(
        "scan_records",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("transfer_requests.id"), nullable=False),
        sa.Column("seal_no", sa.String, nullable=True),
        sa.Column("scan_type", sa.String, nullable=False),
        sa.Column("terminal_id", sa.String, nullable=False),
        sa.Column("occurred_at", sa.String, nullable=False),
        sa.Column("recorded_at", sa.String, nullable=False),
        sa.Column("offline", sa.Integer, nullable=False, server_default="0"),
        sa.Column("result", sa.String, nullable=False),
        sa.Column("idempotency_key", sa.String, nullable=False, unique=True),
    )
    op.create_table(
        "investigations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("transfer_requests.id"), nullable=False),
        sa.Column("reason", sa.String, nullable=False),  # shortage|damage|timeout|bypass
        sa.Column("qty_loss", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String, nullable=False, server_default="open"),
        sa.Column("detail", sa.String, nullable=False, server_default=""),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("resolved_at", sa.String, nullable=True),
    )
    op.create_table(
        "risk_notes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("transfer_requests.id"), nullable=False),
        sa.Column("note", sa.String, nullable=False),
        sa.Column("author", sa.String, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
    )
    op.create_table(
        "todo_tasks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("transfer_requests.id"), nullable=True),
        sa.Column("task_type", sa.String, nullable=False),  # timeout_scan|manual_review
        sa.Column("status", sa.String, nullable=False, server_default="pending"),  # pending|done|kept
        sa.Column("due_at", sa.String, nullable=False),
        sa.Column("payload", sa.String, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("handled_at", sa.String, nullable=True),
    )
    # 事件链只追加：历史节点不可修改/删除，纠正只能写新事件
    op.execute(
        """
        CREATE TRIGGER trg_custody_no_update
        BEFORE UPDATE ON custody_events
        BEGIN
            SELECT RAISE(ABORT, 'custody_events is append-only: updates are forbidden');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_custody_no_delete
        BEFORE DELETE ON custody_events
        BEGIN
            SELECT RAISE(ABORT, 'custody_events is append-only: deletes are forbidden');
        END
        """
    )


def downgrade() -> None:
    op.drop_table("todo_tasks")
    op.drop_table("risk_notes")
    op.drop_table("investigations")
    op.drop_table("scan_records")
    op.drop_table("seal_confirmations")
    op.drop_index("ix_custody_idempotency", table_name="custody_events")
    op.drop_index("ix_custody_singleton_received", table_name="custody_events")
    op.drop_index("ix_custody_singleton_released", table_name="custody_events")
    op.drop_index("ix_custody_request_seq", table_name="custody_events")
    op.drop_index("ix_custody_batch_time", table_name="custody_events")
    op.drop_table("custody_events")
    op.drop_table("request_reviews")
    op.drop_table("transfer_requests")
    op.drop_table("drug_batches")
    op.drop_table("carriers")
    op.drop_table("staff")
    op.drop_table("stores")
