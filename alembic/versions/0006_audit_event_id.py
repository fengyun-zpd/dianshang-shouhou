"""add stable event identifiers to audit events

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE audit_events ADD COLUMN event_id TEXT")
    op.execute("UPDATE audit_events SET event_id = 'audit-' || id::text WHERE event_id IS NULL")
    op.execute("CREATE UNIQUE INDEX uq_audit_event_id ON audit_events(event_id)")


def downgrade() -> None:
    op.execute("DROP INDEX uq_audit_event_id")
    op.execute("ALTER TABLE audit_events DROP COLUMN event_id")
