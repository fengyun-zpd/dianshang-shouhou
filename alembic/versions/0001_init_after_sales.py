"""initial after-sales schema

Revision ID: 0001
Revises:
Create Date: 2026-09-04
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DDL = """
CREATE TABLE IF NOT EXISTS orders (
    tenant_id        TEXT        NOT NULL,
    order_id         TEXT        NOT NULL,
    customer_id      TEXT        NOT NULL,
    status           TEXT        NOT NULL,
    paid_amount      NUMERIC(12,2) NOT NULL,
    days_since_sign  INTEGER     NOT NULL DEFAULT 0,
    version          INTEGER     NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, order_id)
);
CREATE TABLE IF NOT EXISTS tickets (
    tenant_id    TEXT NOT NULL,
    ticket_id    TEXT NOT NULL,
    order_id     TEXT NOT NULL,
    customer_id  TEXT NOT NULL,
    request_type TEXT NOT NULL,
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL,
    resolution   TEXT,
    version      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, ticket_id),
    FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id)
);
CREATE TABLE IF NOT EXISTS refund_operations (
    tenant_id        TEXT NOT NULL,
    operation_id     TEXT NOT NULL,
    ticket_id        TEXT NOT NULL,
    order_id         TEXT NOT NULL,
    op_type          TEXT NOT NULL,
    amount           NUMERIC(12,2),
    status           TEXT NOT NULL,
    idempotency_key  TEXT,
    created_by       TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    decision_version INTEGER,
    executed         BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (tenant_id, operation_id),
    FOREIGN KEY (tenant_id, ticket_id) REFERENCES tickets(tenant_id, ticket_id)
);
CREATE TABLE IF NOT EXISTS approval_decisions (
    id              SERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    operation_id    TEXT NOT NULL,
    decision        TEXT NOT NULL,
    reason          TEXT,
    decided_by      TEXT NOT NULL,
    decided_version INTEGER NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS idempotency_records (
    tenant_id     TEXT NOT NULL,
    idem_key      TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    refund_id     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, idem_key)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    action          TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    actor           TEXT NOT NULL,
    before_state    TEXT,
    after_state     TEXT,
    idempotency_key TEXT,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_entity ON audit_events(tenant_id, entity_type, entity_id);
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    for table in ("audit_events", "idempotency_records", "approval_decisions",
                  "refund_operations", "tickets", "orders"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
