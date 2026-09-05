# 阶段四验收报告（PG-first 运行时与跨进程恢复闭环）

> 归属：OpsPilot 电商售后多智能体工单系统（`D:\workplace\PyCharmMiscProject\私域`）
> 性质：阶段四（PG-first 运行时、审批事实与跨进程恢复闭环）验收记录。**只陈述本机实测事实**，
> 不把规划/未运行内容写成已实现。
> 版本：v1.0（2026-09-05）

## 1. 验收环境（实测）

| 项 | 值 |
| --- | --- |
| 代码版本（git HEAD） | `191889d`（阶段 A 端口收敛 + R7 与文档 v0.46 同步后） |
| 工作树 | 干净（`git diff --check` = 空，`git status --short` 无输出） |
| Python / pytest | 3.12.10 / 9.1.1（D 盘 `.venv`；命令一律 `-p no:cacheprovider` 规避中文路径 cache 失败） |
| PostgreSQL | 本地容器 `opspilot-pg`（127.0.0.1:5433，库/用户/密码 opspilot）；Alembic schema 版本 0005 |
| 运行模式 | 合成数据固定种子 42；无真实 PII/支付/CRM/企业微信/MuleSoft/生产凭证；无真实 LLM 调用 |

## 2. 验收命令与真实输出（2026-09-05 本机实测）

```powershell
# 全量顺序测试
.venv\Scripts\python.exe -m pytest tests/ -q -p no:cacheprovider
# → 441 passed, 1 warning in 26.57s（0 xfailed）

# 分层回归（验收入口）
.venv\Scripts\python.exe scripts\run_tests.py
# → unit/integration/e2e/property/security/regression/phase4 各退出码 0 + 全量 441 passed → REGRESSION PASS

# 分层收集数（--collect-only 实测）
# unit 335 / integration 37 / e2e 2 / property 12 / security 4 / regression 11 / phase4 26 / 根层 14 = 441

# 评测与回放（含 PG profile，2026-09-05 实测）
.venv\Scripts\python.exe evals\replay.py --dataset golden_v1              # memory → 11/11 通过（引用 0.6667）
.venv\Scripts\python.exe evals\replay.py --dataset golden_v1 --profile pg  # PG 事实源 → 11/11 通过（memory vs pg 55 字段 0 差异）
.venv\Scripts\python.exe evals\replay.py --dataset golden_v2              # memory → 120/120 通过
.venv\Scripts\python.exe evals\compare_agents.py               # → 单 Agent 11/11 vs Supervisor 11/11（维持单 Agent）
.venv\Scripts\python.exe evals\run_model_shadow_eval.py --mode offline  # → 意图准确率 1.0，0 降级，未联网

# 测试隔离（每 worker 独立 database：opspilot_p4a/p4b）
powershell -ExecutionPolicy Bypass -File scripts\run_pg_tests_isolated.ps1
# → 两库各自从空库 Alembic 0001→0005 全程升级成功，各跑 PG 集成 25 passed → ISOLATED DOUBLE-RUN PASS（真实退出码 0）
# 说明：脚本为顺序双库验证（非真并行双进程）；"每 worker 独立 database" 保证对象级隔离
```

## 3. 阶段四完成项（对应 objective 各节，均有测试证据）

### 3.1 审批事实与幂等三元组（RED→GREEN，见 `tests/phase4/test_phase4_red_contracts.py`）
- approve/reject 落 `approval_decisions` 事实：tenant/operation/decided_version/decided_by/decision/时间。
- 同操作重复审批 → 唯一约束拒绝且无副作用（HTTP 409 不增行，`test_api_pg_profile_live.py`）。
- 幂等三元组 `(tenant_id, command_type, raw_key)` 唯一：同载荷返回原结果；异载荷拒绝；不同 command_type 可同原始 key（Alembic 0005）。
- 政策解析只用业务时间下最新有效版本（未来/历史失效版本不参与）。
- create_ticket 的 customer_id 以订单事实为准，不匹配稳定拒绝。

### 3.2 Runner 内部强制租约（D9 收口，`tests/phase4/test_runner_lease_live.py` + `test_d9_workflow_lease_live.py`）
- `workflow_threads` 表：tenant/thread/fingerprint/status/lease_owner/lease_until/generation/更新时间。
- `claim_thread`：INSERT..ON CONFLICT 原子获租/续租/过期接管；request_fingerprint 不可变（异 fp 拒绝不覆盖）。
- `WorkflowRunner(lease_repo, owner_id)`：读 checkpoint/update_state/invoke 前必须持约；失约 `ThreadLeaseError` 零副作用；
  finished/异常由 owner 释放、等待审批保持、崩溃后过期可接管（真实双 Runner 竞争 live 2 项通过）。
- checkpoint 仅存流程恢复状态；业务/审批/结果一律从 PG 重读（既有持久 checkpoint + PG live 覆盖）。

### 3.3 端口运行时切换（阶段 A 收敛，commit `4eb2614`）
- `AfterSalesApplicationPort` 扩展 tenant-first 只读（get_ticket/get_operation/get_order/list_customer_tickets/
  compute_refund_plan/audit_log/list_operations），`MemoryAdapter` 与 `PgCommandAdapter` 完整实现同一契约。
- FastAPI/Gateway/Runner 只依赖 Port（不 isinstance 分支）；删除 `PgServiceFacade` 及其裸 ID 全表扫描租户定位路径。
- `scripts/run_api.py --backend pg` 真实装配：PostgresAfterSalesRepository + PgCommandService + PgCommandAdapter +
  持久 SQLite checkpoint + `WorkflowRunner(lease_repo, owner_id)`；PG 不可达/schema 不符 → 退出码非 0 绝不回退 memory；
  启动不自动 seed（`tests/integration/test_run_api_pg_backend_live.py` 2 项：装配链 + 零业务数据）。
- PG profile API e2e：真实 HTTP（建单→草稿→提交→审批→执行）走 PgCommandService，SQL 断言 executed/approval=1
  `decided_by='approver-1'`/execute 审计/幂等三元组落库（`test_api_pg_profile_live.py` 2 项）。
- pg profile approve/reject 缺 expected_version → 422 `EXPECTED_VERSION_REQUIRED`，服务端不代填（R6）。

### 3.4 阶段四 xfail 归零
- 原先 5 个 strict xfail（R1R2/R3R4/R5/R6/R7 + PA1/PA3/PA4 收敛缺口）全部按真实实现转 PASS；
- `tests/phase4` 现 **26 passed, 0 xfailed**（含 PA2/PA3/PA4、R7、list_operations 双后端行为用例）。

## 4. 安全不变量（实测 0 违例）

| 不变量 | 结果 |
| --- | --- |
| 越权成功数 | 0（跨租户 tenant-first 门禁：memory 403 TENANT_MISMATCH / PG 404 NOT_FOUND，均安全拒绝） |
| 重复副作用数 | 0（幂等三元组 + 唯一约束 + Runner 租约） |
| 未知状态盲目重试数 | 0（unknown 仅原 operation_id 对账，禁止换键） |
| 非法状态迁移数 | 0（rules + DB CHECK + 版本 CAS） |
| 审批伪造数 | 0（decided_by=认证 principal；Agent 无 approver 写通道） |

## 5. 未实现 / 未实测（如实声明，不写成已实现）

- **真并行双进程隔离运行**：隔离脚本为顺序双库验证（每 worker 独立 database 已实现；真并行双进程未落地，如实标注）。
- 真实 LLM 影子评测：offline 规则基线已实测；真实模型需用户提供安全 Key/允许 Base URL 后进入影子评测，当前未联网未实测。
- Supervisor 维持实验性可选运行时（A/B 无明确业务收益，ADR-002 回退条款生效）。
- 真实 MuleSoft/MCP 网络接入、微调（LoRA/QLoRA/DPO）：无授权不连接/不训练，未实测。
- 审批工作台前端、API 限流/CORS/OpenAPI 示例：规划中（阶段 D），未实现。

> 补充（2026-09-05，commit `02bdbc8`）：**golden replay 的 PG profile 已落地**——`evals/replay.py --profile pg` 在 PG 事实源真实回放 golden_v1 = 11/11，memory vs pg 逐条 55 字段 0 差异，报告 run_mode 如实标注 pg；memory 回放无回归（v1 11/11、v2 120/120）。全量测试随之升至 **441 passed, 0 xfailed**。

## 6. 风险与遗留（诚实）

1. 内存 vs PG 对「存在但异租户 id」查询错误码差异：Memory=403 `TENANT_MISMATCH`、PG=404 `NOT_FOUND`；
   均为安全拒绝无越权；PG 端复现 403 需引入全局扫描（违背 tenant-first 设计），故维持现状。
2. `collect_audit_events` 审计水位跨租户交替收集与旧实现等价（V1 单流程演示无影响；docstring 已注明边界）。
3. 隔离脚本退出码经管道外层观察曾显示 1：核实为宿主管道噪声，脚本自身真实退出码 0（`script-exit=0` 实测）。

## 7. 结论

阶段四目标（PG-first 运行时、审批事实、跨进程恢复闭环、端口收敛、文档同步）在本机达成验收：
全量 439 passed / 0 xfailed、PG profile HTTP e2e 通过、并发租约/幂等/审批执行通过、隔离 double-run PASS、
`git diff --check=0` 工作树干净。未完成项见 §5，均如实标注，不声称真实生产接入/真实 LLM/微调/真实收益。
