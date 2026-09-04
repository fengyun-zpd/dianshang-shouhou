# Task K1：订单级退款并发一致性（设计说明）

> 归属：电商售后多智能体工单系统（OpsPilot）。版本：v1.0（2026-09-04）。
> 范围：仅领域一致性（`src/domain/after_sales/service.py` 与测试）；不改 API/PostgreSQL/LLM/RAG/Supervisor/Mule/微调。

## 1. 复现的并发缺陷

同一订单实付 `100.00`，两个**不同幂等键**并发/顺序各退款 `60.00`：

- 两个草稿都能创建（per-key 幂等锁只串行同一键，不同键各自通过剩余金额检查）；
- 两个都审批通过；
- 两个 `execute(success)` 都成功 → 累计 `120.00` **超退**。

修复前可复现：`_approved_refund(60)` ×2 后依次 `execute` 两个操作，第二个同样成功（无容量守卫）。

## 2. 采用方案：订单级锁 + 执行阶段原子容量校验（选择理由）

| 候选 | 取舍 |
| --- | --- |
| A. 草稿阶段预占金额（reserved 池） | 需在审批拒绝/取消/失败时释放预占，状态机与回滚复杂；草稿误占可能阻塞本可执行的小额退款 |
| **B. 执行阶段原子校验（采用）** | 真实副作用只发生在 `execute(success)` 与 `reconcile(success)`；在这两处于订单锁内做“已执行累计 + 本次 ≤ 实付”的原子裁决。失败即抛明确错误码（`AMOUNT_EXCEEDS_REMAINING`），**不迁移状态、不累计、不伪造成功**；审批通过但额度不足的操作保持 `APPROVED`/`UNKNOWN`，供人工改小额或驳回/对账失败收口 |

## 3. 订单级锁语义

- 锁键 `(tenant_id, order_id)`（租户在键内，天然隔离跨租户）；
- **临界段**（在同一订单锁内完成，满足任务要求 2 的六项）：
  - `create_refund`：工单状态 → 幂等命中 → **订单级 unknown 存在性检查**（同键原 unknown 由幂等命中返回，换新键必拒 `OPERATION_UNKNOWN_CONFLICT`）→ 剩余金额检查 → 创建；
  - `execute`：unknown 语义守卫 → **容量校验** → 状态迁移（EXECUTED/UNKNOWN）→ `refunded` 累计 → 审计；
  - `reconcile`：unknown 原键守卫 → **容量校验**（对账成功等价最终执行，防多 unknown 对账越界）→ EXECUTED/FAILED → 累计/审计；
  - `close_ticket`：与执行/创建交错安全（防“关闭中又追加操作”竞态）。
- **锁顺序**：需持多把锁的路径一律**先订单锁、后幂等键锁**（`create_refund` 内部 per-key 锁为任务 J 的 CAS），全局一致避免死锁；不同订单互不阻塞。

## 4. 不变量与错误码

- 无论并发顺序，已执行退款累计 ≤ 实付（execute 与 reconcile-success 均在锁内容量校验）；
- 同订单两个已审批操作并发执行 → **至多一个成功**，另一个抛 `AMOUNT_EXCEEDS_REMAINING` 且保持 `APPROVED`（明确非成功状态，不伪造）；
- unknown 只能原操作对账收口：换新键创建 → `OPERATION_UNKNOWN_CONFLICT`；同键同载荷重复 → 幂等返回原操作（幂等判定先于金额/unknown 守卫，修复“已执行后同键重复被金额误拒”）；
- 失败/超额执行不增加 `refunded`；
- 同键同载荷返回原结果、异载荷拒绝（`IDEMPOTENCY_CONFLICT`）语义不变。

## 5. 验证

- RED→GREEN：`tests/unit/domain/after_sales/test_order_refund_concurrency.py`（7 项：顺序超退拒绝 / 并发执行单成功 / unknown 对账 vs 执行竞争 / 换键拒绝 / 同键重复零副作用 / 超额执行不累计 / timeout 不累计）。
- 全量：`.venv\Scripts\python.exe -m pytest tests/ -q` → **242 passed**；`scripts/run_tests.py` → `REGRESSION PASS`；`evals/replay.py` → 当时 **10/11**（g10 重复请求契约过期：业务已收敛为“重复请求返回原结果”，契约仍期望 `already_executed`）；该契约在 K2 评测收敛后已同步，恢复 11/11。

## 6. 未验证 / 后续

- 内存锁为**进程内单实例**语义；跨进程/多实例需 **PostgreSQL 行锁迁移**（`SELECT ... FOR UPDATE` 或 `INSERT ... ON CONFLICT` + 行级容量检查），本实现不声称生产级数据库事务；
- 未接入真实支付/退款系统；审批并发（approve 与 execute 交错）依赖操作状态机的原子迁移，未做专门的 approve 并发压测。
