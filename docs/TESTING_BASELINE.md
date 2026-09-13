# 测试基线

## 本轮实测（V1.2 收口，2026-09-13，D 盘 `.venv` home=D:\Anaconda，Python 3.12.4）

```powershell
. .\scripts\init_d_env.ps1
# 运行模式 A（离线）：不设置 OPSPILOT_TEST_DATABASE_URL → PG live 破坏性集成按纪律 skip
.venv\Scripts\python.exe -m pytest tests -q
# → 490 passed，49 skipped，1 条第三方弃用警告（本次实测，10.6 s）

# 运行模式 B（隔离 PG）：空库迁移至 alembic head 0005 后跑全量
$env:OPSPILOT_TEST_DATABASE_URL = 'postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot_test_final_6003af47'
.venv\Scripts\python.exe -m pytest tests -q
# → 539 passed，0 skipped，1 条第三方弃用警告（本次实测，30.1 s）

# 隔离双跑（官方入口，自动创建两套带随机后缀的测试库）
.\scripts\run_pg_tests_isolated.ps1
# → opspilot_test_a_74ae988e / opspilot_test_b_74ae988e 各 25 passed；ISOLATED DOUBLE-RUN PASS
```

两模式差异 = 49 个 PG live 用例（integration/phase4/regression 中经
`src/platform/pg_test_guard.py` 守卫的破坏性集成；本轮新增 4 个：Agent HTTP 主链路 2 个、
跨进程重启恢复 2 个跨租户/租约用例）。未设置隔离库时的 49 个 skip 仅说明 PG live 未启用，
不能与隔离结果混用。**历史轮次的数字（399/44、443/0、462/44、481/46、506/0、527/0）属于当时
提交的实测记录，已被本轮取代，不得再作为当前基线引用。**

## 本轮（V1.2 收口）测试增补

| 文件 | 项数 | 覆盖 |
| --- | --- | --- |
| `tests/e2e/test_agent_http_lifecycle.py` | 29 | start/clarify/decision/state 正常与失败路径；身份边界（客户 403）；`extra="forbid"`（tenant_id/金额/角色/审批/外部结果 → 422）；跨租户统一 404 且不泄露租户；同名线程跨租户不冲突；SYSTEM 写 unknown 后 decision 重读；伪造 decision 422；线程冲突 409；reset profile 边界；503；循环错误码 |
| `tests/integration/test_agent_restart_recovery_live.py` | 3（PG live） | 实例 A 中断 → 关闭 → 实例 B 用同一 PG + 固定 checkpoint 读 state / 恢复 decision / 继续执行；错误租户 404；同名线程跨租户；租约未过期时拒绝推进 |
| `tests/integration/test_run_api_pg_backend_live.py` | 4（PG live） | PG profile 使用真实 PG runner + D9 租约；不注册 memory reset；start→审批事实→decision→state 全链路以 `refund_operations` 行验收；伪造 decision body 422 |
| `tests/unit/agents/test_loop_protection.py` | 16 | step_count 增长；超限安全停止；重复澄清有界；空澄清载荷 no-op；工具去重账本；事实重读不缓存；recursion limit 第二道防线；**循环终态入持久 checkpoint（新实例可读）**；安全停止后再 resume 不产生新写入；unknown 不换键重试 |
| `tests/unit/agents/test_thread_tenant_namespace.py` | 3 | `(tenant_id, thread_id)` 命名空间；错误租户与不存在线程的通用 404（消息除线程名外一致）；新实例显式租户恢复 |
| `tests/unit/models/test_openai_compatible.py` | 20（含 3 项新增） | 请求构造/降级/脱敏/注入/预算；**证据块脱敏后才计数与发送（断言请求体无手机号/邮箱/身份证）**；日志无 PII；注入检查仍先于脱敏 |
| `tests/unit/models/test_cost_tracking.py` | 21 | 新/旧价格变量、双向成本、缺价 N/A、非法价格安全回退、报告成本列、Key 不入报告 |
| `tests/unit/models/test_shadow_zero_side_effect.py` | 5 | 不创建领域/数据库对象、零网络、高风险任务不进模型、预测字段不含金额/审批（全部写 `tmp_path`） |
| `tests/unit/evals/test_llm_judge.py` | 19 | rubric 加载/失败、PII 脱敏、不触发领域写、无 Key 不联网、业务错误不被高分掩盖、真实裁判 mock 路径 |

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
