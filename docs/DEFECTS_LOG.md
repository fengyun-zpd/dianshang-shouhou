# 缺陷台账（DEFECTS LOG）

> 版本：v1.0（2026-09-04）。第一阶段产物：先补回归测试并执行，记录每项 pass/缺陷证据，
> **不做大面积重构**。修复按第二阶段起逐项进行；修复后把对应 XFAIL 测试改为 PASS 并更新本表。
> 证据来源：`tests/regression/test_defect_ledger_regressions.py`（**11 passed, 0 xfailed**——
> D1–D12 全部修复转 PASS，2026-09-04 实测，PG 容器运行中）+ 既有测试引用。
> 严重级：P0=阻断（数据覆盖/跨租户/分叉），P1=并发/恢复/审计完整性问题。

## 总览

| 编号 | 缺陷/回归项 | 现状 | 证据 | 严重级 | 归属阶段 |
| --- | --- | --- | --- | --- | --- |
| D1 | 跨租户同 `order_id` 不互相覆盖 | **已修复（提交 阶段二）** | PASS `test_d1_*`（seed 跨租户同 order_id 显式拒绝 fail-closed 不静默覆盖；多租户共存权威语义由 PG `(tenant_id, order_id)` 主键承载） | P0 | 二 ✅ |
| D2 | 跨租户同 `idempotency_key` 互不冲突 | **已修复（提交 阶段二）** | PASS `test_d2_*`（命令内幂等键统一为 `f"{tenant}:{key}"` 租户前缀规范键：store/审计/操作实体同值；跨租户同原始 key 各自成功、同租户幂等语义保留） | P0 | 二 ✅ |
| D3 | 客户只能读取自己的工单 | **已补强（提交 阶段三第一步）** | PASS `test_customer_cannot_read_other_customers_ticket` / `test_customer_can_read_own_ticket_only` / `test_d3_*`——API `GET /tickets/{id}` 增加 CUSTOMER 资源级授权（越权 → 403 `AFTER_SALES_PERMISSION_DENIED`，响应不含目标工单字段/电话）；原 `test_customer_only_own_ticket` 仅覆盖创建侧，不作为完整证据 | — | 三 ✅（资源授权） |
| D4 | 审批与拒绝携带 `expected_version` | **已修复（提交 阶段二）** | PASS `test_d4_*`（`RejectCommand.decision_version`；过期版本拒绝→409 语义；拒绝亦推进版本） | P1 | 二 ✅ |
| D5 | 并发审批只有一个成功 | 部分 | PASS `test_d5_*`（顺序同版本二次审批被拒=版本 CAS 有效）；**并发真双跑**缺操作级锁/DB CAS | P1 | 二（op 级锁/DB CAS） |
| D6 | PG `with_order_lock` 业务期间保持锁 | **已修复（提交 阶段二）** | PASS `test_d6_*`（FOR UPDATE 事务保持至 yield 体完成；contender 阻塞至持有者提交 dt≈0.6s） | P0 | 二 ✅ |
| D7 | PG 保存失败内存与 DB 不分叉 | **已修复（提交 阶段三第五步收口）** | PASS `test_d7_*`——生产命令路径（`PgCommandService`）不调用 clear_all/镜像重插（源码断言）；命令失败整事务回滚零残留由 PG live 实证（`test_live_no_partial_commit_on_validation_failure` 等）；`PgBackedSession` 全量镜像写仅作兼容迁移工具并如实标注 | P0 | 三 ✅ |
| D8 | 重启后政策/订单明细/审批/审计完整恢复 | **已修复（提交 阶段三）** | PASS `test_d8_*` + live（`PgBackedSession.load()` 无参自 policies/order_items 表完整恢复政策与明细；Alembic 0004 + PolicyRow/OrderItemRow 装载方法；审批/审计/幂等此前已保真） | P1 | 三 ✅ |
| D9 | 两进程同时 resume 同一线程单推进 | 未实现 | 无跨进程租约/DB 锁（设计项；单实例重复 resume 幂等已有测试保障） | P1 | 四（workflow_threads+租约） |
| D10 | unknown 仅原 `operation_id` 对账 | 通过 | PASS `test_d10_*`（换新键 → `OPERATION_UNKNOWN_CONFLICT`；原键 reconcile 成功） | — | 保持 |
| D11 | 外部未知不自动换键重试 | 通过 | PASS `test_d11_*`（timeout→unknown，无二次 create_refund 审计） | — | 保持 |
| D12 | 报告生成无随机时间差异 | **已修复（提交 阶段三第七步前哨）** | PASS `test_d12_*`（`compare_agents.py` 报告移除 P50/P95 与逐 case 耗时列，结论仅由通过率决定；两次运行报告哈希相同=跨运行零 diff） | P1 | 七 ✅ |

## 逐项证据与修复方向（简）

- **D1/D2（P0，第二阶段）**：`src/domain/after_sales/service.py` 索引 `_orders/_tickets/_operations` 为裸键
  （seed/写入路径），`IdempotencyStore`（`src/domain/idempotency.py`）`_records` 为裸 key。方向：
  全部领域索引与幂等键改为 `(tenant_id, entity_id/key)` 作用域；Repository 层已是 `(tenant, id)` 主键。
- **D4/D5（P1）**：`RejectCommand` 增加 `decision_version` 并在 `reject()` 校验；并发审批需操作行
  版本 CAS（`update_operation_versioned` 已具 DB CAS 语义，命令路径接入之）。
- **D6（P0）**：`src/repo/postgres.py::with_order_lock` 读取行后 `tx.commit()` 即释放锁；须改为
  锁保持至上下文退出（业务代码在未提交事务内执行，退出时提交/回滚），与 `unit_of_work` 语义一致。
- **D7（P0）**：`PgBackedSession` 为命令后全量 clear/reinsert 镜像写：save 失败时内存（已被命令
  mutate）与 DB（回滚旧镜像）分叉。方向：第二阶段废弃该生产路径，改为 PG-first 命令事务
  （第三阶段 command service），失败整体回滚并重读事实。
- **D8（P1）**：政策与订单明细不入表；需要 policy/order_items 持久化（表+迁移）或等价装载机制，
  实现"重启完整恢复"。审批（approval_decisions）/审计/幂等已保真（既有 live 测试）。
- **D9（P1）**：第四阶段 `workflow_threads` 表 + resume 租约/DB 锁后补 two-process 测试。
- **D12（P1）**：对照报告移除/稳定化逐 run 耗时列（保留确定性指标），第七阶段复跑验证零 diff。
