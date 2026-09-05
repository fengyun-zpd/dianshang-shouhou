"""add ticket meta columns (created_by / reason_tags)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-04
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PG-backed 领域会话（全量镜像写）需要工单保真恢复：创建者角色与诉求标签入表
    op.execute("ALTER TABLE tickets ADD COLUMN created_by TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE tickets ADD COLUMN reason_tags TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE tickets DROP COLUMN reason_tags")
    op.execute("ALTER TABLE tickets DROP COLUMN created_by")
