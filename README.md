# OpsPilot

> 企业售后工单处置 Agent 与可靠性评测项目。
> 版本：V1.1 Release Candidate（2026-09-06）。

## 项目使命

构建可面试展示、可复现的**单 Agent 售后退款可靠性工作流**：证明 Agent 能在证据不足、越权、
重复请求、工具失败和外部结果未知时安全停止、等待或转人工；金额、资格、状态、审批与最终
事实永远由确定性领域服务 + PostgreSQL 裁决。

## V1.1 主张

OpsPilot 证明 Agent 可以在受控权限内组织一条退款工单流程，并在证据不足、越权、重复请求、工具失败和外部结果未知时安全停止、等待或转人工。

- 单 Agent 负责意图、澄清、只读证据检索和方案解释。
- 确定性领域服务负责金额、资格、状态、权限、幂等、并发与审计。
- 高风险动作先生成草稿，只有授权人员提交审批决定后才可执行。
- PostgreSQL 是业务事实源；SQLite checkpoint 只保存流程恢复位置。
- **确定性证据检索基线**（本地 PolicyStore 检索：租户隔离、版本有效、引用定位、注入拒绝、无证据转人工）在 Agent 回放中提供政策 citation；不能裁决金额、资格或状态，也不能替代 PostgreSQL 事实。

## V1.1 范围

保留单 Agent LangGraph（默认路径）、确定性证据检索基线、PostgreSQL 命令路径、黄金集、回放和演示。Supervisor 只保留 A/B 实验结论：与单 Agent 无量化业务收益，因此默认不用；V1.1 新增**四角色只读多 Agent 可选编排**（Triage/Evidence/Resolution/RiskReview，`SupervisorRunner(orchestration="four-role")`），每角色轨迹（trace_id/输入输出摘要/耗时/工具调用/citation/拒绝原因）经 `scripts/demo_supervisor_trace.py` 可复现查看；`evals/compare_modes.py` 以同 golden_v1 对照 single/three-agent/four-role（11/11 持平 → 维持单 Agent）。运行模式开关见 `src/agents/modes.py`：`single_agent`（**默认**）/`multi_agent`（四角色实验）/`offline_rule`（无 LLM Key 自动离线规则）/`llm`（仅显式配置安全 Key + 白名单 Base URL 才可用，未配置安全回落 offline_rule）。FastAPI 当前验证的是领域服务接口；Agent 启动、澄清和恢复仍由脚本/回放入口驱动（**API 与 Agent 运行器是两个入口**，未合并为 HTTP 主链路）。

不纳入 V1.1：真实 LLM 指标、真实微调效果（仅实验入口/合成样本，无 GPU 未运行）、真实 Mule/MCP、前端工作台、pgvector、长期记忆和生产部署。

```text
请求 -> 订单/租户核验 -> 政策证据与澄清 -> 规则计算 -> 动作草稿
     -> 人工审批 interrupt -> 重读事实 resume -> 执行或 operation_unknown
     -> 原 operation_id 对账 -> 关单与审计
```

## 当前验证

2026-09-06，在 D 盘运行时环境实测（`.venv` home=D:\Anaconda，Python 3.12.4）：

```powershell
. .\scripts\init_d_env.ps1
# 运行模式 A：未配置隔离 PG 库 → PG live 破坏性集成按纪律 skip：
.venv\Scripts\python.exe -m pytest tests/ -q
# 394 passed, 44 skipped, 1 warning
# 运行模式 B：OPSPILOT_TEST_DATABASE_URL 指向 opspilot_test_* 隔离库 → PG live 全实测：
# 438 passed, 0 skipped
.venv\Scripts\python.exe scripts\demo_interview.py
# 七个演示场景全部通过（五核心 + 跨租户/重复请求）
.venv\Scripts\python.exe scripts\demo_rag_policy.py
# 确定性证据检索基线展示全部通过
.venv\Scripts\python.exe scripts\demo_supervisor_trace.py --mode multi_agent
# Supervisor 四角色轨迹演示（8 场景，含每角色耗时/输入输出摘要/工具调用/citation）
.venv\Scripts\python.exe evals\compare_modes.py
# 单 Agent / three-agent / four-role 同 golden_v1 对照（11/11 持平 → 默认单 Agent）
```

44 个跳过项均为未配置或不可达隔离 PostgreSQL 的 live 测试，**不代表 PG 已验证**（配置隔离
库后 438 全绿才是 PG live 实测）。数据为固定种子合成数据，不代表真实企业收益；真实模型、
微调效果、生产连接和性能均未实测。破坏性 PG 集成只允许 `opspilot_test_*`@localhost
（`OPSPILOT_TEST_DATABASE_URL`），共享主库永不被 DROP（见 `src/platform/pg_test_guard.py`）。

## D 盘约束

`.venv` 和基础解释器位于 D 盘。运行前执行 `scripts/init_d_env.ps1`，它只为当前 PowerShell 设置 `TEMP`、`TMP`、pytest 临时目录、pip 缓存和 Python 字节码缓存，统一写入项目 `.runtime/` 与 `.cache/`。禁止向 C 盘安装、下载、缓存或写项目运行时文件。

## 七场景演示与可复现命令

```powershell
. .\scripts\init_d_env.ps1
.venv\Scripts\python.exe scripts\demo_interview.py    # 七场景（五核心 + 跨租户/重复请求）
.venv\Scripts\python.exe scripts\demo_rag_policy.py   # 确定性证据检索基线展示
.venv\Scripts\python.exe scripts\demo_modes.py        # 运行模式开关冒烟
```

七场景（真实断言）：① 正常破损退款闭环；② 缺订单号 → 澄清；③ 无政策证据 → 转人工不猜测；
④ 审批拒绝 → 零副作用；⑤ 外部 unknown → 仅原键对账；⑥ 跨租户拒绝；⑦ 重复请求幂等返回原结果。
黄金集：`.venv\Scripts\python.exe evals\replay.py --dataset golden_v1`（11/11）；
对照：`.venv\Scripts\python.exe evals\compare_agents.py`（单 Agent vs Supervisor，无收益维持单 Agent）；
评测：`.venv\Scripts\python.exe evals\rag_metrics.py`、`evals\run_model_shadow_eval.py --mode offline`。

## 面试版项目介绍（90 秒）

> "OpsPilot 是企业售后的确定性可靠性 Agent：单 Agent 只做意图、澄清与只读证据检索；
> 金额、资格、状态、审批、幂等与审计由确定性领域服务处理，业务事实落在 PostgreSQL。
> 退款先生成草稿、授权人带版本审批后才恢复执行，checkpoint 只存流程状态；幂等防重复、
> 外部 unknown 只按原操作对账。检索基线只给可追溯政策 citation、不裁决金额。V1.1 提供
> 四角色只读多 Agent 与运行模式开关，但同黄金集 A/B 无收益，故默认单 Agent；真实 LLM 可
> 经安全 Key 接入但当前未接入——离线规则保证任何时刻可运行。"

## 文档

- [工程宪法](./AGENTS.md)
- [架构](./docs/ARCHITECTURE.md)
- [PostgreSQL 事实源](./docs/POSTGRES.md)
- [测试基线](./docs/TESTING_BASELINE.md)
- [面试讲解](./docs/INTERVIEW_OVERVIEW.md)
- [V1 执行方案与 Agent 提示词](./docs/V1_EXECUTION_PLAN.md)
- [Supervisor 实验](./docs/MULTI_AGENT_EXPERIMENT.md)
- [模型与微调门禁](./docs/MODEL_EVALUATION.md)
