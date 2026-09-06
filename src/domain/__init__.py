"""确定性领域服务层。

对齐宪法第三条职责分离：权限、金额、状态迁移、幂等、并发、审计全部在此确定性完成，
不包含任何 LLM 逻辑。Agent 只能通过命令对象间接影响本层，且不能放行写操作。

包结构（如实）：
- `src/domain/after_sales/` —— **主业务实现**：售后工单处置（AfterSalesService +
  rules + pg_commands + ports/adapters），面向 PostgreSQL 业务事实源与 memory/pg 双后端；
  新业务能力一律加在这里。
- `src/domain/models.py`（Role / parse_money 等共享规约）/ `idempotency.py` ——
  被 after_sales 与 api/agents/platform 只读复用（单一来源）；`Role` 不另起炉灶。
- 早期退款最小闭环（`refund_service.py` 及旧命令模型）已随 V1 收口删除，不复存在。
"""
