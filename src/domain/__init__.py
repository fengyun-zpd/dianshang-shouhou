"""确定性领域服务层。

对齐宪法第三条职责分离：权限、金额、状态迁移、幂等、并发、审计全部在此确定性完成，
不包含任何 LLM 逻辑。Agent 只能通过命令对象间接影响本层，且不能放行写操作。

包结构（如实）：
- `src/domain/after_sales/` —— **主业务实现**：售后工单处置（AfterSalesService +
  rules + pg_commands + ports/adapters），面向 PostgreSQL 业务事实源与 memory/pg 双后端；
  新业务能力一律加在这里。
- `src/domain/refund_service.py` / `models.py`（Role 等共享规约）/ `idempotency.py` ——
  LEGACY：早期最小闭环，保留作历史回归基线（`tests/test_refund_service.py`）与演进对照，
  不用于生产路径，不要新增业务能力。`Role` 等共享规约被 after_sales/bridge 复用（单一来源）。
"""
