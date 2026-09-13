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
    PRIMARY KEY (tenant_id, order_id),
    CONSTRAINT ck_orders_paid_nonneg CHECK (paid_amount >= 0)
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
    created_by   TEXT NOT NULL DEFAULT '',
    reason_tags  TEXT,
    PRIMARY KEY (tenant_id, ticket_id),
    FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id),
    CONSTRAINT ck_tickets_status CHECK (status IN ('open','resolved','rejected','closed'))
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
    FOREIGN KEY (tenant_id, ticket_id) REFERENCES tickets(tenant_id, ticket_id),
    CONSTRAINT ck_refundop_status CHECK (status IN (
        'draft','pending_approval','approved','rejected','executed','unknown','failed')),
    CONSTRAINT ck_refundop_amount CHECK (amount IS NULL OR amount > 0)
);

-- 审批决定（授权人员提交，带版本；同一 (tenant, operation, decided_version) 只允许一个决定）
CREATE TABLE IF NOT EXISTS approval_decisions (
    id              SERIAL PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    operation_id    TEXT NOT NULL,
    decision        TEXT NOT NULL,           -- approved / rejected
    reason          TEXT,
    decided_by      TEXT NOT NULL,           -- 认证身份 principal（不可伪造的 actor_id）
    decided_version INTEGER NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_approval_operation_version
        UNIQUE (tenant_id, operation_id, decided_version)
);

-- 幂等记录：幂等三元组 (tenant_id, command_type, raw_key) 唯一（0005）；
-- idem_key 保留为 D2 前缀规范键（展示/审计冗余列，非唯一键）
CREATE TABLE IF NOT EXISTS idempotency_records (
    tenant_id     TEXT NOT NULL,
    idem_key      TEXT NOT NULL,
    command_type  TEXT NOT NULL DEFAULT '',
    raw_key       TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    refund_id     TEXT NOT NULL,
    CONSTRAINT uq_idem_three_tuple UNIQUE (tenant_id, command_type, raw_key)
);

-- 审计（追加式；不含 PII）
CREATE TABLE IF NOT EXISTS audit_events (
    id              BIGSERIAL PRIMARY KEY,
    event_id        TEXT,
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
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_event_id ON audit_events(event_id) WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_tenant_entity ON audit_events(tenant_id, entity_type, entity_id);

-- 订单级退款额度并发保护：执行/对账成功前
-- SELECT ... FOR UPDATE ON orders WHERE tenant_id=? AND order_id=?
-- 累计已执行退款 = SELECT COALESCE(SUM(amount),0) FROM refund_operations
--   WHERE tenant_id=? AND order_id=? AND status='executed'
-- 容量校验：sum + 本次 <= paid_amount，否则拒绝（与领域错误码 AFTER_SALES_AMOUNT_EXCEEDS_REMAINING 对应）。

-- ============ Alembic 0004：PG-first 命令服务数据模型（阶段三） ============
-- 政策（含版本与生效期；单一政策多版本行，命令/恢复按 tenant+request_type+生效期取用）
CREATE TABLE IF NOT EXISTS policies (
    tenant_id     TEXT NOT NULL,
    policy_id     TEXT NOT NULL,
    request_type  TEXT NOT NULL,
    reason_tags   TEXT NOT NULL,            -- JSON 数组字符串，如 '["damaged"]'
    window_days   INTEGER NOT NULL,
    refund_ratio  NUMERIC(5,4) NOT NULL,
    effective_from DATE NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, policy_id, version),
    CONSTRAINT ck_policies_ratio CHECK (refund_ratio > 0 AND refund_ratio <= 1),
    CONSTRAINT ck_policies_window CHECK (window_days >= 0),
    CONSTRAINT ck_policies_effective CHECK (effective_from IS NOT NULL)
);

-- 订单明细（订单快照展示/证据完整恢复；退款上限仍以 orders.paid_amount 为准）
CREATE TABLE IF NOT EXISTS order_items (
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
);

-- 租户作用域自增序列（替代内存全局 _seq；跨进程安全 id 分配）
CREATE TABLE IF NOT EXISTS entity_seq (
    tenant_id TEXT NOT NULL,
    kind      TEXT NOT NULL,                -- 'ticket' | 'operation'
    next_val  BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, kind),
    CONSTRAINT ck_entity_seq_kind CHECK (kind IN ('ticket', 'operation'))
);

-- ============ Alembic 0005：阶段四（跨进程工作流线程与租约；checkpoint 仍只存流程状态） ============
CREATE TABLE IF NOT EXISTS workflow_threads (
    tenant_id          TEXT NOT NULL,
    thread_id          TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',   -- pending/active/finished/abandoned
    request_fingerprint TEXT NOT NULL DEFAULT '',
    lease_owner        TEXT,                              -- 当前持租约的 runner（principal/进程）
    lease_until        TIMESTAMPTZ,                       -- 租约到期时间；过期后才能被接管
    generation         INTEGER NOT NULL DEFAULT 1,        -- 版本/代数（接管或状态推进 +1）
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    recovery_meta      TEXT,                              -- JSON：节点/最小恢复元数据（不含业务真相）
    PRIMARY KEY (tenant_id, thread_id)
);
