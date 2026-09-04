-- OpsPilot 售后工单：PostgreSQL 唯一事实源 Schema（K4）
-- 说明：本 schema 为生产路线（唯一事实源）的 DDL；SQLite 仅为恢复原型，不能替代。
-- 所有业务表带 tenant_id（租户范围）；金额用 numeric(12,2)；幂等键有数据库唯一约束。

-- 订单快照（领域服务所需只读事实；版本用于乐观并发）
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

-- 工单
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

-- 退款操作（含 operation_unknown 语义状态）
CREATE TABLE IF NOT EXISTS refund_operations (
    tenant_id        TEXT NOT NULL,
    operation_id     TEXT NOT NULL,
    ticket_id        TEXT NOT NULL,
    order_id         TEXT NOT NULL,
    op_type          TEXT NOT NULL,
    amount           NUMERIC(12,2),
    status           TEXT NOT NULL,          -- draft/pending_approval/approved/rejected/executed/unknown/failed
    idempotency_key  TEXT,
    created_by       TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    decision_version INTEGER,
    executed         BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (tenant_id, operation_id),
    FOREIGN KEY (tenant_id, ticket_id) REFERENCES tickets(tenant_id, ticket_id)
);

-- 审批决定（授权人员提交，带版本）
CREATE TABLE IF NOT EXISTS approval_decisions (
    id              SERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    operation_id    TEXT NOT NULL,
    decision        TEXT NOT NULL,           -- approved / rejected
    reason          TEXT,
    decided_by      TEXT NOT NULL,
    decided_version INTEGER NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 幂等记录：同 (tenant_id, key) 唯一 → 数据库级唯一约束
CREATE TABLE IF NOT EXISTS idempotency_records (
    tenant_id     TEXT NOT NULL,
    idem_key      TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    refund_id     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, idem_key)
);

-- 审计（追加式；不含 PII）
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

-- 订单级退款额度并发保护：执行/对账成功前
-- SELECT ... FOR UPDATE ON orders WHERE tenant_id=? AND order_id=?
-- 累计已执行退款 = SELECT COALESCE(SUM(amount),0) FROM refund_operations
--   WHERE tenant_id=? AND order_id=? AND status='executed'
-- 容量校验：sum + 本次 <= paid_amount，否则拒绝（与领域错误码 AFTER_SALES_AMOUNT_EXCEEDS_REMAINING 对应）。
