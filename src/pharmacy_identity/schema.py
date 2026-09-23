"""表结构元数据：迁移与应用共用的唯一事实来源。

所有时间均以带时区的 UTC ISO-8601 字符串存储；保管事件只增不改，
由 APPEND_ONLY_TRIGGERS 中的 SQLite 触发器在数据库层强制执行。
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

metadata = MetaData()

# --- 主体与身份 -----------------------------------------------------------

parties = Table(
    "parties",
    metadata,
    Column("code", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("type", String, nullable=False),
    Column("jurisdiction", String, nullable=False),
    Column("created_at", String, nullable=False),
    CheckConstraint("type IN ('STORE','CARRIER','QUARANTINE','REGULATOR')", name="ck_parties_type"),
)

actors = Table(
    "actors",
    metadata,
    Column("actor_id", String, primary_key=True),
    Column("display_name", String, nullable=False),
    Column("role", String, nullable=False),
    Column("party_code", String, ForeignKey("parties.code"), nullable=True),
    Column("can_review", Boolean, nullable=False, server_default="0"),
    Column("active", Boolean, nullable=False, server_default="1"),
    Column("created_at", String, nullable=False),
    CheckConstraint("role IN ('ADMIN','STORE_STAFF','CARRIER_STAFF','AUDITOR')", name="ck_actors_role"),
)

# 许可证与关系均为版本化行：纠正时不覆盖旧行，旧行置 CORRECTED 并另插新版本。
licenses = Table(
    "licenses",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("party_code", String, ForeignKey("parties.code"), nullable=False),
    Column("license_type", String, nullable=False),
    Column("license_number", String, nullable=False),
    Column("valid_from", String, nullable=False),
    Column("valid_to", String, nullable=False),
    Column("status", String, nullable=False),
    Column("corrected_at", String, nullable=True),
    Column("created_at", String, nullable=False),
    CheckConstraint("status IN ('ACTIVE','SUSPENDED','REVOKED','EXPIRED','CORRECTED')", name="ck_licenses_status"),
    Index("ux_licenses_version", "party_code", "license_type", "valid_from", unique=True),
)

relationships = Table(
    "relationships",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("party_code", String, ForeignKey("parties.code"), nullable=False),
    Column("related_party_code", String, ForeignKey("parties.code"), nullable=False),
    Column("relation_type", String, nullable=False),
    Column("valid_from", String, nullable=False),
    Column("valid_to", String, nullable=False),
    Column("status", String, nullable=False),
    Column("corrected_at", String, nullable=True),
    Column("created_at", String, nullable=False),
    CheckConstraint(
        "relation_type IN ('STORE_CARRIER','SENDER_RECEIVER','JURISDICTION')",
        name="ck_relationships_type",
    ),
    CheckConstraint("status IN ('ACTIVE','INACTIVE','CORRECTED')", name="ck_relationships_status"),
    Index(
        "ux_relationships_version",
        "party_code",
        "related_party_code",
        "relation_type",
        "valid_from",
        unique=True,
    ),
)

# --- 药品与批次 -----------------------------------------------------------

drugs = Table(
    "drugs",
    metadata,
    Column("product_code", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("controlled_class", String, nullable=False),
    Column("created_at", String, nullable=False),
)

batches = Table(
    "batches",
    metadata,
    Column("product_code", String, ForeignKey("drugs.product_code"), primary_key=True),
    Column("batch_number", String, primary_key=True),
    Column("initial_qty", Integer, nullable=False),
    Column("expiry_date", String, nullable=True),
    Column("holder_party_code", String, ForeignKey("parties.code"), nullable=False),
    Column("registered_at", String, nullable=False),
    CheckConstraint("initial_qty > 0", name="ck_batches_qty"),
)

quarantine_locations = Table(
    "quarantine_locations",
    metadata,
    Column("code", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("jurisdiction", String, nullable=False),
    Column("created_at", String, nullable=False),
)

# --- 调拨申请与冻结快照 ----------------------------------------------------

transfer_requests = Table(
    "transfer_requests",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_number", String, nullable=False, unique=True),
    Column("sender_code", String, ForeignKey("parties.code"), nullable=False),
    Column("receiver_code", String, ForeignKey("parties.code"), nullable=False),
    Column("carrier_code", String, ForeignKey("parties.code"), nullable=False),
    Column("planned_dispatched_at", String, nullable=False),
    Column("planned_received_at", String, nullable=False),
    Column("status", String, nullable=False),
    Column("snapshot_json", Text, nullable=False),
    Column("snapshot_valid", Boolean, nullable=False, server_default="1"),
    Column("revalidated_at", String, nullable=True),
    Column("seal_code", String, nullable=True),
    Column("driver_name", String, nullable=True),
    Column("vehicle_no", String, nullable=True),
    Column("created_by", String, ForeignKey("actors.actor_id"), nullable=False),
    Column("created_at", String, nullable=False),
    Column("dispatched_at", String, nullable=True),
    Column("received_at", String, nullable=True),
    Column("cancelled_at", String, nullable=True),
    Column("rejected_at", String, nullable=True),
    Column("returned_at", String, nullable=True),
    Column("reject_reason", String, nullable=True),
    CheckConstraint(
        "status IN ('PLANNED','DISPATCHED','DELIVERED','REJECTED','RETURNED','CANCELLED','QUARANTINED')",
        name="ck_requests_status",
    ),
)

transfer_lines = Table(
    "transfer_lines",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", Integer, ForeignKey("transfer_requests.id"), nullable=False),
    Column("product_code", String, nullable=False),
    Column("batch_number", String, nullable=False),
    Column("planned_qty", Integer, nullable=False),
    Column("received_qty", Integer, nullable=False, server_default="0"),
    Column("damaged_qty", Integer, nullable=False, server_default="0"),
    Column("shortage_qty", Integer, nullable=False, server_default="0"),
    Column("quarantined_qty", Integer, nullable=False, server_default="0"),
    Column("returned_qty", Integer, nullable=False, server_default="0"),
    ForeignKeyConstraint(
        ["product_code", "batch_number"], ["batches.product_code", "batches.batch_number"]
    ),
    Index("ux_lines_request_batch", "request_id", "product_code", "batch_number", unique=True),
)

reviews = Table(
    "reviews",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", Integer, ForeignKey("transfer_requests.id"), nullable=False),
    Column("review_role", String, nullable=False),
    Column("actor_id", String, ForeignKey("actors.actor_id"), nullable=False),
    Column("decision", String, nullable=False),
    Column("note", String, nullable=True),
    Column("created_at", String, nullable=False),
    CheckConstraint("review_role IN ('FIRST','SECOND')", name="ck_reviews_role"),
    CheckConstraint("decision IN ('APPROVE','REJECT')", name="ck_reviews_decision"),
    Index("ux_reviews_request_role", "request_id", "review_role", unique=True),
)

# --- 保管事件链（追加型）---------------------------------------------------

custody_events = Table(
    "custody_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", Integer, ForeignKey("transfer_requests.id"), nullable=True),
    Column("seq", Integer, nullable=True),
    Column("event_type", String, nullable=False),
    Column("actor_id", String, ForeignKey("actors.actor_id"), nullable=True),
    Column("device_id", String, nullable=True),
    Column("occurred_at", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    Column("holder_type", String, nullable=False),
    Column("holder_code", String, nullable=False),
    Column("seal_code", String, nullable=True),
    Column("location", String, nullable=True),
    Column("idempotency_key", String, nullable=True),
    Column("reason", String, nullable=True),
    Column("note", String, nullable=True),
    Column("payload_json", Text, nullable=True),
    Column("prev_hash", String, nullable=True),
    Column("event_hash", String, nullable=True),
    CheckConstraint(
        "event_type IN ("
        "'BASELINE','REQUESTED','REVIEWED','CANCELLED','DISPATCHED','SCAN',"
        "'RECEIVED','REJECTED','QUARANTINED','RETURNED','INVESTIGATION_RESOLVED')",
        name="ck_events_type",
    ),
    CheckConstraint(
        "holder_type IN ('STORE','CARRIER','QUARANTINE','LOSS')",
        name="ck_events_holder",
    ),
    Index("ux_events_request_seq", "request_id", "seq", unique=True),
)

# 同一封签的签收确认全局只能成功一次（双终端竞争由数据库裁决）；
# 出库封签同样不得复用；设备扫码按 (device, 幂等键) 去重。
Index(
    "ux_events_received_seal",
    custody_events.c.seal_code,
    unique=True,
    sqlite_where=custody_events.c.event_type == "RECEIVED",
)
Index(
    "ux_events_dispatch_seal",
    custody_events.c.seal_code,
    unique=True,
    sqlite_where=custody_events.c.event_type == "DISPATCHED",
)
Index(
    "ux_events_scan_idem",
    custody_events.c.device_id,
    custody_events.c.idempotency_key,
    unique=True,
    sqlite_where=custody_events.c.idempotency_key.isnot(None),
)

custody_items = Table(
    "custody_items",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", Integer, ForeignKey("custody_events.id"), nullable=False),
    Column("product_code", String, nullable=False),
    Column("batch_number", String, nullable=False),
    Column("qty", Integer, nullable=False),
    Index("ux_items_event_batch", "event_id", "product_code", "batch_number", unique=True),
)

# --- 调查、风险说明与人工复核 ----------------------------------------------

investigations = Table(
    "investigations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("case_number", String, nullable=False, unique=True),
    Column("request_id", Integer, ForeignKey("transfer_requests.id"), nullable=False),
    Column("reason", String, nullable=False),
    Column("status", String, nullable=False),
    Column("opened_by", String, ForeignKey("actors.actor_id"), nullable=True),
    Column("opened_at", String, nullable=False),
    Column("resolved_at", String, nullable=True),
    Column("outcome", String, nullable=True),
    Column("note", String, nullable=True),
    CheckConstraint("reason IN ('SHORTAGE','DAMAGE','TIMEOUT','SEAL_MISMATCH','MANUAL')", name="ck_investigations_reason"),
    CheckConstraint("status IN ('OPEN','RESOLVED')", name="ck_investigations_status"),
    CheckConstraint(
        "outcome IS NULL OR outcome IN ('RELEASE_TO_RECEIVER','RETURN_TO_SENDER','CONFIRM_LOSS')",
        name="ck_investigations_outcome",
    ),
)

risk_notes = Table(
    "risk_notes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", Integer, ForeignKey("transfer_requests.id"), nullable=False),
    Column("category", String, nullable=False),
    Column("note", Text, nullable=False),
    Column("actor_id", String, ForeignKey("actors.actor_id"), nullable=False),
    Column("created_at", String, nullable=False),
    CheckConstraint(
        "category IN ('LICENSE_CORRECTED','RELATIONSHIP_CORRECTED','OTHER')",
        name="ck_risk_category",
    ),
)

manual_reviews = Table(
    "manual_reviews",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", Integer, ForeignKey("transfer_requests.id"), nullable=True),
    Column("reason", String, nullable=False),
    Column("status", String, nullable=False),
    Column("payload_json", Text, nullable=True),
    Column("created_by", String, ForeignKey("actors.actor_id"), nullable=True),
    Column("created_at", String, nullable=False),
    Column("resolved_at", String, nullable=True),
    Column("resolution", String, nullable=True),
    Column("resolved_by", String, ForeignKey("actors.actor_id"), nullable=True),
    CheckConstraint(
        "reason IN ('NODE_SKIP','CHAIN_ORDER','SEAL_MISMATCH','OTHER')",
        name="ck_manual_reason",
    ),
    CheckConstraint("status IN ('PENDING','RESOLVED')", name="ck_manual_status"),
)

# HTTP 幂等：同一幂等键重放首次响应。
idempotent_requests = Table(
    "idempotent_requests",
    metadata,
    Column("idempotency_key", String, primary_key=True),
    Column("request_fingerprint", String, nullable=False),
    Column("response_status", Integer, nullable=False),
    Column("response_body", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

APPEND_ONLY_TABLES = ("custody_events", "custody_items", "reviews", "risk_notes")


def append_only_triggers() -> list[str]:
    """生成追加型表的防改防删触发器 DDL。"""
    statements: list[str] = []
    for table in APPEND_ONLY_TABLES:
        statements.append(
            f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} 为追加型记录，禁止修改'); END"
        )
        statements.append(
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} 为追加型记录，禁止删除'); END"
        )
    return statements
