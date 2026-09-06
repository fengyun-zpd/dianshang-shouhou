# OpsPilot V1 架构

```text
FastAPI（当前已验证：领域服务接口与审批/审计边界）
  -> AfterSalesApplicationPort
       |-- MemoryAdapter（测试/演示）
       `-- PgCommandAdapter（PG profile）
             -> PgCommandService（权限/金额/状态/幂等/审批/审计/租约）
             -> PostgreSQL（业务事实源）
WorkflowRunner（脚本/回放驱动的单 Agent 流程）
  -> 只读证据、确定性证据检索基线（本地 RAG，可选接入）、澄清、审批 interrupt/resume
SQLite checkpoint（仅流程状态）
```

`src/domain/after_sales/` 是唯一业务主实现。早期退款服务、Mule Bridge 和整库镜像持久化原型已删除，不参与默认运行时。

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| 单 Agent | 意图、缺参澄清、只读证据、解释 | 金额、资格、状态、审批、执行放行 |
| 确定性证据检索基线（本地 RAG） | 合成政策 citation 与注入信号；租户/版本/引用约束、无证据转人工 | 金额、资格、状态或事实源版本裁决 |
| 领域服务 | 权限、金额、状态、幂等、并发、审计 | 自然语言理解 |
| 授权人员 | 带版本 approve/reject 决定 | 直接改写业务事实 |
| checkpoint | 流程挂起与恢复 | 订单、金额、审批、执行结果 |

默认验收路径是确定性领域 API 加单 Agent 回放/演示。当前 FastAPI 工厂没有接入 WorkflowRunner，不能宣称 HTTP 已覆盖 Agent 启动、澄清和恢复；若不补这层，面试叙述应明确二者是两个入口。Supervisor 只作 A/B 实验；同一黄金集下没有收益，因此不进入默认路径。真实 LLM 与微调均未实测/未实现。

```powershell
cd D:\workplace\PyCharmMiscProject\私域
. .\scripts\init_d_env.ps1
.venv\Scripts\python.exe -m pytest tests/ -q
.venv\Scripts\python.exe scripts\demo_interview.py
.venv\Scripts\python.exe scripts\run_api.py --backend memory
```
