"""保管链路表结构（SQLAlchemy Core 镜像，DDL 由 Alembic 迁移管理）。"""

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    text,
)

metadata = MetaData()

stores = Table(
    "stores",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", String, nullable=False, unique=True),
    Column("name", String, nullable=False),
    Column("license_no", String, nullable=False),
    Column("license_status", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

staff = Table(
    "staff",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("user_code", String, nullable=False, unique=True),
    Column("store_id", ForeignKey("stores.id"), nullable=False),
    Column("role", String, nullable=False),
    Column("active", Integer, nullable=False, server_default="1"),
)

carriers = Table(
    "carriers",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", String, nullable=False, unique=True),
    Column("name", String, nullable=False),
    Column("qualification_no", String, nullable=False),
    Column("qualification_status", String, nullable=False),
    Column("qualified_until", String, nullable=True),
    Column("updated_at", String, nullable=False),
)

drug_batches = Table(
    "drug_batches",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("batch_no", String, nullable=False, unique=True),
    Column("drug_name", String, nullable=False),
    Column("controlled", Integer, nullable=False, server_default="1"),
)

transfer_requests = Table(
    "transfer_requests",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("request_no", String, nullable=False, unique=True),
    Column("batch_no", String, nullable=False),
    Column("drug_name", String, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("from_store_id", ForeignKey("stores.id"), nullable=False),
    Column("to_store_id", ForeignKey("stores.id"), nullable=False),
    Column("carrier_id", ForeignKey("carriers.id"), nullable=False),
    Column("planned_at", String, nullable=False),
    Column("expected_by", String, nullable=False),
    Column("status", String, nullable=False),
    Column("frozen_snapshot", String, nullable=False),
    Column("seal_no", String, nullable=True),
    Column("received_qty", Integer, nullable=True),
    Column("requires_manual_review", Integer, nullable=False, server_default="0"),
    Column("created_by", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("released_at", String, nullable=True),
    Column("completed_at", String, nullable=True),
)

request_reviews = Table(
    "request_reviews",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("request_id", ForeignKey("transfer_requests.id"), nullable=False),
    Column("reviewer_user_code", String, nullable=False),
    Column("decision", String, nullable=False),
    Column("comment", String, nullable=True),
    Column("created_at", String, nullable=False),
    UniqueConstraint("request_id", "reviewer_user_code", name="uq_review_one_per_reviewer"),
)

custody_events = Table(
    "custody_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("request_id", ForeignKey("transfer_requests.id"), nullable=True),
    Column("batch_no", String, nullable=False),
    Column("seq", Integer, nullable=False),
    Column("event_type", String, nullable=False),
    Column("from_custodian", String, nullable=True),
    Column("to_custodian", String, nullable=True),
    Column("quantity", Integer, nullable=False, server_default="0"),
    Column("occurred_at", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    Column("actor", String, nullable=False),
    Column("terminal_id", String, nullable=True),
    Column("source", String, nullable=False, server_default="online"),
    Column("idempotency_key", String, nullable=True),
    Column("payload", String, nullable=False, server_default="{}"),
    Index("ix_custody_batch_time", "batch_no", "occurred_at"),
    Index("ix_custody_request_seq", "request_id", "seq"),
    Index(
        "ix_custody_idempotency",
        "idempotency_key",
        unique=True,
        sqlite_where=text("idempotency_key IS NOT NULL"),
    ),
    Index(
        "ix_custody_singleton_released",
        "request_id",
        "event_type",
        unique=True,
        sqlite_where=text("event_type = 'released'"),
    ),
    Index(
        "ix_custody_singleton_received",
        "request_id",
        "event_type",
        unique=True,
        sqlite_where=text("event_type = 'received'"),
    ),
)

seal_confirmations = Table(
    "seal_confirmations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("request_id", ForeignKey("transfer_requests.id"), nullable=False),
    Column("seal_no", String, nullable=False),
    Column("terminal_id", String, nullable=False),
    Column("confirmed_by", String, nullable=False),
    Column("occurred_at", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    UniqueConstraint("request_id", "seal_no", name="uq_seal_confirmed_once"),
)

scan_records = Table(
    "scan_records",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("request_id", ForeignKey("transfer_requests.id"), nullable=False),
    Column("seal_no", String, nullable=True),
    Column("scan_type", String, nullable=False),
    Column("terminal_id", String, nullable=False),
    Column("occurred_at", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    Column("offline", Integer, nullable=False, server_default="0"),
    Column("result", String, nullable=False),
    Column("idempotency_key", String, nullable=False, unique=True),
)

investigations = Table(
    "investigations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("request_id", ForeignKey("transfer_requests.id"), nullable=False),
    Column("reason", String, nullable=False),
    Column("qty_loss", Integer, nullable=False, server_default="0"),
    Column("status", String, nullable=False, server_default="open"),
    Column("detail", String, nullable=False, server_default=""),
    Column("created_at", String, nullable=False),
    Column("resolved_at", String, nullable=True),
)

risk_notes = Table(
    "risk_notes",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("request_id", ForeignKey("transfer_requests.id"), nullable=False),
    Column("note", String, nullable=False),
    Column("author", String, nullable=False),
    Column("created_at", String, nullable=False),
)

todo_tasks = Table(
    "todo_tasks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("request_id", ForeignKey("transfer_requests.id"), nullable=True),
    Column("task_type", String, nullable=False),
    Column("status", String, nullable=False, server_default="pending"),
    Column("due_at", String, nullable=False),
    Column("payload", String, nullable=False, server_default="{}"),
    Column("created_at", String, nullable=False),
    Column("handled_at", String, nullable=True),
)
