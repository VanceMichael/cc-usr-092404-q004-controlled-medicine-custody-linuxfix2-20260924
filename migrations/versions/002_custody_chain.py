"""建立受控药品保管链全部数据表。"""

import sys
from pathlib import Path

from alembic import op

# 允许在未 pip install -e 的环境（本地直接跑 alembic）中导入应用包。
_SRC = Path(__file__).resolve().parents[2] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pharmacy_identity.schema import APPEND_ONLY_TABLES, append_only_triggers, metadata  # noqa: E402

revision = "002_custody_chain"
down_revision = "001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind())
    for ddl in append_only_triggers():
        op.execute(ddl)


def downgrade() -> None:
    bind = op.get_bind()
    for table in APPEND_ONLY_TABLES:
        bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
        bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
    metadata.drop_all(bind=bind)
