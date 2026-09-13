# 缺陷回归台账

本台账对应 `tests/regression/test_defect_ledger_regressions.py`。它记录需要长期防回归的
安全与一致性语义，不把历史阶段名称、已删除实现或旧测试总数当作当前结论。

| 编号 | 回归语义 | 当前保护 |
| --- | --- | --- |
| D1 | 跨租户同订单号不发生静默覆盖 | 内存路径 fail-closed；PG 使用 `(tenant_id, order_id)` 作用域 |
| D2 | 幂等键按租户隔离 | 同租户同载荷复用结果；跨租户不冲突 |
| D3 | 客户只能读取自己的工单 | 资源级授权和领域查询双重覆盖 |
| D4-D5 | 审批/拒绝使用版本 CAS | 过期决定拒绝；并发只允许一个有效决定 |
| D6 | 订单锁覆盖整个业务临界区 | PG 事务锁与并发回归测试 |
| D7 | 命令失败不产生内存与数据库分叉 | PG-first 命令事务；生产路径禁止整库清空重插 |
| D8 | 订单明细和政策可从事实表恢复 | `policies`、`orders`、`order_items` 行编解码与 PG 命令路径 |
| D9 | 同一工作流线程只能单推进 | `workflow_threads` 租约、指纹和失约零副作用 |
| D10-D11 | 外部结果未知只可按原操作对账 | 新键拒绝；不自动重试写副作用 |
| D12 | 对照报告可复现 | 报告不含逐次运行耗时，结论只依赖确定性指标 |

验证规则：全量回归、黄金集和演示分别记录在 `TESTING_BASELINE.md` 与运行输出中。PostgreSQL
不可用时，必须将 PG 相关验证标记为未验证，不能用内存结果替代。

## V1.2 补充审查闭环（2026-09-13）

| 编号 | 缺陷 | 整改结论 | 回归证据 |
| --- | --- | --- | --- |
| R1 | checkpoint 分隔符碰撞 | 已修复：v2 长度编码，旧键精确匹配，歧义拒绝 | `tests/unit/agents/test_thread_tenant_namespace.py`、`review_v12_boundaries.py` R1-*、`test_agent_http_lifecycle.py::test_colon_collision_http_is_isolated_and_write_free`（零业务写入）、`test_agent_restart_recovery_live.py::test_pg_colon_collision_start_then_read_stays_isolated`（PG：先 start 后 GET） |
| R2 | HTTP 流程视图泄露 PII | 已修复：公共字段白名单和递归脱敏，错误响应同样处理 | `test_agent_http_lifecycle.py::test_public_agent_views_and_validation_errors_redact_pii`、`::test_clarify_decision_views_and_logs_redact_pii`（含日志）、`::test_request_fingerprint_distinguishes_raw_text_with_same_public_view`（脱敏不合并不同原始请求） |
| R3 | 审计跨线程/租户遗漏或混入 | 已修复：按线程实体过滤并稳定去重 | `tests/unit/agents/test_audit_association.py`（5 项：跨租户交错、同租户交错审批、重复读取、新实例重建、领域事实可重建）、`review_v12_boundaries.py` R3-* |
| R4 | unknown/已执行缺少恢复收尾 | 已修复：原操作恢复、领域事实收口、只允许合法关单 | `tests/unit/agents/test_resume_idempotency_and_unknown.py`（含关单失败保留错误码/重试收尾）、`review_v12_boundaries.py` R4-* |
| R5 | 重启测试租约偶发 409 | 已修复测试证据：独立子进程 + DB 时间等待租约过期 | `tests/integration/test_agent_restart_recovery_live.py`（独立进程 A/B；循环终态跨实例存活） |

回归有效性：临时回退 R1/R3 对应实现后，上述新增回归确实失败（audit 2 failed、namespace 3 failed、
PG collision 1 failed），恢复源码后全部通过；明细见 `docs/TESTING_BASELINE.md`。

本轮闭环的完整历史复现和实测数字见 [V1.2 补充审查](./V1_2_REVIEW_2026-09-13.md)。
