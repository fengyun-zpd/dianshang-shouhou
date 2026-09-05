"""add PG-first command data model (policies / order_items / entity_seq)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-04
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE policies (
            tenant_id     TEXT NOT NULL,
            policy_id     TEXT NOT NULL,
            request_type  TEXT NOT NULL,
            reason_tags   TEXT NOT NULL,
            window_days   INTEGER NOT NULL,
            refund_ratio  NUMERIC(5,4) NOT NULL,
            effective_from DATE NOT NULL,
            version       INTEGER NOT NULL DEFAULT 1,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, policy_id, version),
            CONSTRAINT ck_policies_ratio CHECK (refund_ratio > 0 AND refund_ratio <= 1),
            CONSTRAINT ck_policies_window CHECK (window_days >= 0)
        )
    """)
    op.execute("""
        CREATE TABLE order_items (
            tenant_id   TEXT NOT NULL,
            order_id    TEXT NOT NULL,
            sku         TEXT NOT NULL,
            name        TEXT NOT NULL,
            quantity    INTEGER NOT NULL,
            unit_price  NUMERIC(12,2) NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, order_id, sku),
            FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id),
            CONSTRAINT ck_order_items_qty CHECK (quantity > 0),
            CONSTRAINT ck_order_items_price CHECK (unit_price >= 0)
        )
    """)
    op.execute("""
        CREATE TABLE entity_seq (
            tenant_id TEXT NOT NULL,
            kind      TEXT NOT NULL,
            next_val  BIGINT NOT NULL DEFAULT 1,
            PRIMARY KEY (tenant_id, kind),
            CONSTRAINT ck_entity_seq_kind CHECK (kind IN ('ticket', 'operation'))
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS entity_seq")
    op.execute("DROP TABLE IF EXISTS order_items")
    op.execute("DROP TABLE IF EXISTS policies")
