"""phase4: approval uniqueness / idempotency 3-tuple / workflow_threads

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-05
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) approval_decisions：同一 (tenant, operation, decided_version) 只允许一个决定
    #    （approve 推进版本；同版本重复/并发决定被 DB 唯一拒绝 → 无重复审批副作用）
    op.execute("""
        ALTER TABLE approval_decisions
        ADD CONSTRAINT uq_approval_operation_version
        UNIQUE (tenant_id, operation_id, decided_version)
    """)
    # 2) idempotency_records：幂等语义改为 (tenant_id, command_type, raw_key) 三元组。
    #    旧主键 (tenant_id, idem_key) 移除；idem_key 保留为展示/审计冗余列（D2 前缀规范键）。
    #    raw_key 回填：无法从带前缀的旧键无损剥离，迁移按原值回填并置 command_type=''（默认），
    #    由新代码按三元组写入（演示/测试数据可接受；生产从 0005 起全新写入）。
    op.execute("ALTER TABLE idempotency_records DROP CONSTRAINT idempotency_records_pkey")
    op.execute("ALTER TABLE idempotency_records ADD COLUMN command_type TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE idempotency_records ADD COLUMN raw_key TEXT")
    op.execute("UPDATE idempotency_records SET raw_key = idem_key WHERE raw_key IS NULL")
    op.execute("ALTER TABLE idempotency_records ALTER COLUMN raw_key SET NOT NULL")
    op.execute("""
        ALTER TABLE idempotency_records
        ADD CONSTRAINT uq_idem_three_tuple UNIQUE (tenant_id, command_type, raw_key)
    """)
    # 3) workflow_threads：跨进程工作流线程事实（D9 租约载体；checkpoint 仍只存流程状态）
    op.execute("""
        CREATE TABLE workflow_threads (
            tenant_id          TEXT NOT NULL,
            thread_id          TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'pending',
            request_fingerprint TEXT NOT NULL DEFAULT '',
            lease_owner        TEXT,
            lease_until        TIMESTAMPTZ,
            generation         INTEGER NOT NULL DEFAULT 1,
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            recovery_meta      TEXT,
            PRIMARY KEY (tenant_id, thread_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE workflow_threads")
    op.execute("ALTER TABLE idempotency_records DROP CONSTRAINT uq_idem_three_tuple")
    op.execute("ALTER TABLE idempotency_records DROP COLUMN raw_key")
    op.execute("ALTER TABLE idempotency_records DROP COLUMN command_type")
    op.execute("""
        ALTER TABLE idempotency_records ADD CONSTRAINT idempotency_records_pkey
        PRIMARY KEY (tenant_id, idem_key)
    """)
    op.execute("ALTER TABLE approval_decisions DROP CONSTRAINT uq_approval_operation_version")
