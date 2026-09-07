# 测试基线

## 当前实测（V1.1，2026-09-07，D 盘 `.venv` home=D:\Anaconda）

```powershell
. .\scripts\init_d_env.ps1
# 运行模式 A：不设置 OPSPILOT_TEST_DATABASE_URL → PG live 破坏性集成按纪律 skip：
.venv\Scripts\python.exe -m pytest tests/ -q
# → 399 passed，44 skipped，1 条第三方弃用警告   （2026-09-07 Agent Lab 边界剧本变更后实测）

# 运行模式 B：设置 OPSPILOT_TEST_DATABASE_URL（指向 opspilot_test_* 隔离库）→ PG live 全部实测：
$env:OPSPILOT_TEST_DATABASE_URL = 'postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot_test_v1'
.venv\Scripts\python.exe -m pytest tests/ -q
# → 443 passed，0 skipped，1 条第三方弃用警告（2026-09-07 实测）
```

两模式差异 = 44 个 PG live 用例（integration/phase4/regression 中经
`src/platform/pg_test_guard.py` 守卫的破坏性集成）。隔离脚本在 `opspilot_test_a_012b8bf0` 与
`opspilot_test_b_012b8bf0` 各完成 `25 passed`，均从空库迁移至 `0005`；随后全量测试使用前者得到
`443 passed, 0 skipped, 1 warning`。未设置隔离库时的 44 个 skip 仅说明 PG live 未启用，不能与
隔离结果混用。这些数字不能与清理前的 450 项或更早基线混用。`scripts/demo_interview.py` 七场景全部通过：五核心场景 =
正常闭环 / 信息不足（缺订单澄清）/ 无政策证据转人工 / 审批拒绝零副作用 / 外部结果未知
（unknown 原键对账）；补充安全演示 = 跨租户拒绝 / 重复请求幂等。黄金集回放：memory
`golden_v1` 11/11、`golden_v2` 120/120（同口径：引用正确率仅统计期望命中现行版，安全拒绝
与注入单独计）。确定性证据检索基线展示：`scripts/demo_rag_policy.py`（导入/分块/启停/版本
切换/注入拒绝/引用校验，真实执行）。

测试目录覆盖 unit 规则与适配器（含 **V1.1 四角色多 Agent**：Triage/Evidence/Resolution/
RiskReview，`tests/unit/agents/test_multiagent_four_role.py` 14 项）、integration
PostgreSQL/API/并发/租约、e2e 中断恢复、property 不变量、security 租户/注入/PII/未知状态、
regression 缺陷回归、phase4 PG profile 与 models（受控 LLM 离线基线 35 项）。

```powershell
. .\scripts\init_d_env.ps1
.venv\Scripts\python.exe scripts\run_tests.py --quick
.venv\Scripts\python.exe evals\replay.py
.venv\Scripts\python.exe scripts\demo_interview.py
.venv\Scripts\python.exe scripts\demo_rag_policy.py
```

PG 不可用时必须报告跳过，不能把内存结果写成 PG 实测。任一越权成功、重复副作用、未知状态
换键重试或非法迁移都是阻断问题。破坏性 PG 集成只允许指向 `OPSPILOT_TEST_DATABASE_URL`
（localhost + `opspilot_test_*` 前缀）；共享主库 `opspilot` 与通用 `DATABASE_URL` 永不被
DROP（`src/platform/pg_test_guard.py` fail-closed：非法 URL 在任何连接探测前直接失败）。
