"""add db-level check constraints

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-04
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE orders ADD CONSTRAINT ck_orders_paid_nonneg CHECK (paid_amount >= 0)")
    op.execute("ALTER TABLE tickets ADD CONSTRAINT ck_tickets_status "
               "CHECK (status IN ('open','resolved','rejected','closed'))")
    op.execute("ALTER TABLE refund_operations ADD CONSTRAINT ck_refundop_status "
               "CHECK (status IN ('draft','pending_approval','approved','rejected',"
               "'executed','unknown','failed'))")
    op.execute("ALTER TABLE refund_operations ADD CONSTRAINT ck_refundop_amount "
               "CHECK (amount IS NULL OR amount > 0)")


def downgrade() -> None:
    op.execute("ALTER TABLE orders DROP CONSTRAINT ck_orders_paid_nonneg")
    op.execute("ALTER TABLE tickets DROP CONSTRAINT ck_tickets_status")
    op.execute("ALTER TABLE refund_operations DROP CONSTRAINT ck_refundop_status")
    op.execute("ALTER TABLE refund_operations DROP CONSTRAINT ck_refundop_amount")
