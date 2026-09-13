# 测试基线

> **2026-09-13 整改验收 + 独立复验已完成。** 修复 R1–R5 后本轮独立复验：离线全量
> **502 passed / 55 skipped**，隔离 PG 全量 **557 passed / 0 skipped**，边界脚本 R1–R4 全部 PASS
>（本轮新增稳定审计 `event_id` 迁移与 PG R2–R4 定点回归）。
> （memory 6 项 + PG 1 项，退出码 0）；重启回归由两个独立 Python 进程执行。下方早期
> 490/538/539、494/543 数字与 6/7 FAIL 仍保留为历史审查证据，不能改写为当前结果，
> 详见 [V1.2 补充审查](./V1_2_REVIEW_2026-09-13.md)。

## 本轮实测（V1.2 整改验收复验，2026-09-13，D 盘 `.venv` home=D:\Anaconda，Python 3.12.4）

```powershell
. .\scripts\init_d_env.ps1
# 运行模式 A（离线）：不设置 OPSPILOT_TEST_DATABASE_URL → PG live 破坏性集成按纪律 skip
.venv\Scripts\python.exe -m pytest tests -q
# → 502 passed，55 skipped，1 条第三方弃用警告（0006 迁移后实测，约 11 s）

# 运行模式 B（隔离 PG）：本轮新建并迁移到 alembic head 0006 的唯一命名测试库
$env:OPSPILOT_TEST_DATABASE_URL = 'postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot_test_v12_<随机后缀>'
.venv\Scripts\python.exe -m pytest tests -q
# → 557 passed，0 skipped，1 条第三方弃用警告（0006 迁移后实测，约 37 s）

# 隔离双跑（官方入口，自动创建两套带随机后缀的测试库）
.\scripts\run_pg_tests_isolated.ps1
# → opspilot_test_a_8c9dfc6f / opspilot_test_b_8c9dfc6f 各 25 passed；ISOLATED DOUBLE-RUN PASS
```

两模式差异 = 51 个 PG live 用例（integration/phase4/regression 中经
`src/platform/pg_test_guard.py` 守卫的破坏性集成；本轮新增 2 个 PG 回归：冒号碰撞隔离、
循环终态跨实例存活）。未设置隔离库时的 51 个 skip 仅说明 PG live 未启用，不能与隔离结果混用。
**历史轮次数字以及整改前的 490/538/539、494/543 记录属于当时提交的实测证据，已被本轮取代，
不得再作为当前基线引用。** 隔离库名每轮随机生成，不作为下一轮默认目标。

## 本轮（V1.2 整改验收）测试增补

| 文件 | 项数 | 覆盖 |
| --- | --- | --- |
| `tests/e2e/test_agent_http_lifecycle.py` | 31 | start/clarify/decision/state 正常与失败路径；身份边界（客户 403）；`extra="forbid"`（tenant_id/金额/角色/审批/外部结果 → 422）；跨租户统一 404 且不泄露租户；冒号碰撞 HTTP 隔离且零业务写入；**脱敏后视图相同≠同请求（指纹仍区分原始文本）**；clarify/decision/冲突路径与日志无 PII；SYSTEM 写 unknown 后 decision 重读；伪造 decision 422；线程冲突 409；reset profile 边界；503；循环错误码 |
| `tests/integration/test_agent_restart_recovery_live.py` | 5（PG live） | 两个独立 Python 进程 A/B 中断、租约按数据库时间到期、同一 PG + 固定 checkpoint 恢复；错误租户 404；同名线程跨租户；租约未过期时拒绝推进；**R1 PG 冒号碰撞（先 start 后 GET）隔离且零业务写入**；**循环终态跨实例存活且不得重启执行** |
| `tests/integration/test_run_api_pg_backend_live.py` | 4（PG live） | PG profile 使用真实 PG runner + D9 租约；不注册 memory reset；start→审批事实→decision→state 全链路以 `refund_operations` 行验收；伪造 decision body 422 |
| `tests/unit/agents/test_audit_association.py` | 5 | 审计按租户/线程/实体关联：T1→T2→T1 不串；同租户两线程交错审批不混入；重复 state/decision 不重复累计；新实例重建一致；编号可由领域审计事实逐条重建 |
| `tests/unit/agents/test_loop_protection.py` | 16 | step_count 增长；超限安全停止；重复澄清有界；空澄清载荷 no-op；工具去重账本；事实重读不缓存；recursion limit 第二道防线；**循环终态入持久 checkpoint（新实例可读）**；安全停止后再 resume 不产生新写入；unknown 不换键重试 |
| `tests/unit/agents/test_thread_tenant_namespace.py` | 5 | v2 长度编码命名空间；冒号碰撞；旧键精确元数据兼容与歧义拒绝；错误租户与不存在线程的通用 404；新实例显式租户恢复 |
| `tests/unit/agents/test_resume_idempotency_and_unknown.py` | 5 | 重复 resume 幂等；unknown 原键对账；对账失败保持开放；外部已执行先于恢复时不重复 execute；**关单失败保留原错误码与人工信息、重试后收尾且不重复执行** |
| `tests/unit/models/test_openai_compatible.py` | 20（含 3 项新增） | 请求构造/降级/脱敏/注入/预算；**证据块脱敏后才计数与发送（断言请求体无手机号/邮箱/身份证）**；日志无 PII；注入检查仍先于脱敏 |
| `tests/unit/models/test_cost_tracking.py` | 21 | 新/旧价格变量、双向成本、缺价 N/A、非法价格安全回退、报告成本列、Key 不入报告 |
| `tests/unit/models/test_shadow_zero_side_effect.py` | 5 | 不创建领域/数据库对象、零网络、高风险任务不进模型、预测字段不含金额/审批（全部写 `tmp_path`） |
| `tests/unit/evals/test_llm_judge.py` | 19 | rubric 加载/失败、PII 脱敏、不触发领域写、无 Key 不联网、业务错误不被高分掩盖、真实裁判 mock 路径 |

### 回归有效性的变异验证（本轮）

新回归不是复述实现细节：临时回退对应修复后，测试确实失败，随后恢复源码（工作树确认干净）：

```text
回退 collect_audit_events（改回全局水位的旧实现）→ test_audit_association.py：2 failed, 3 passed
回退 _checkpoint_thread_key（改回 "tenant:thread"） → test_thread_tenant_namespace.py：3 failed, 2 passed
同一回退下 PG live：test_pg_colon_collision_start_then_read_stays_isolated → 1 failed
  （症状与审查记录一致：409 AGENT_THREAD_CONFLICT）
```

## 分层实测命令

```powershell
. .\scripts\init_d_env.ps1
.venv\Scripts\python.exe -m pytest tests/unit/api -q
.venv\Scripts\python.exe -m pytest tests/unit/agents -q
.venv\Scripts\python.exe -m pytest tests/unit/models -q
.venv\Scripts\python.exe -m pytest tests/unit/evals -q
.venv\Scripts\python.exe -m pytest tests/e2e/test_agent_http_lifecycle.py -q
.venv\Scripts\python.exe scripts\run_tests.py --quick
```

## 报告落盘约定（防污染）

- canonical 报告写入 `evals/reports/`：`shadow_eval_offline.md`、`judge_offline.md`、
  `golden_v1_report.md`、`golden_v2_report.md`、`mode_compare.md`、`agent_compare.md`、
  `rag_metrics.json`；
- **未实测的候选模式报告写入 `.runtime/reports/`**（D 盘运行时目录，不入库），
  避免把离线降级写成真实候选模型成绩；
- **单元测试一律使用 `tmp_path`，不得改写 `evals/reports/`**；
  可在跑完整套件前后比对报告文件哈希验证（本轮已验证：哈希不变）。

PG 不可用时必须报告跳过，不能把内存结果写成 PG 实测。任一越权成功、重复副作用、未知状态
换键重试或非法迁移都是阻断问题。破坏性 PG 集成只允许指向 `OPSPILOT_TEST_DATABASE_URL`
（localhost + `opspilot_test_*` 前缀）；共享主库 `opspilot` 与通用 `DATABASE_URL` 永不被
DROP（`src/platform/pg_test_guard.py` fail-closed：非法 URL 在任何连接探测前直接失败）。
