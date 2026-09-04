# 项目现状与风险报告

> 归属：电商售后多智能体工单系统（OpsPilot After-Sales，目标远程 `fengyun-zpd/dianshang-shouhou`）
> 性质：总负责人 Agent 的侦察结论与基线记录，供子代理与后续轮次引用。本文档只陈述事实与判断，不把规划写成已实现。
> 版本：v0.1（首次侦察）

## 1. 工作区与目录角色

| 路径 | 角色 | 规则 |
| --- | --- | --- |
| `D:\workplace\PyCharmMiscProject\私域` | **新项目本地工作区**（OpsPilot 售后工单，宪法与 README 在此） | 允许写新业务代码；当前**不是 git 仓库** |
| `D:\workplace\PyCharmMiscProject\仓储` | StockMind V1 参考基线 + 新项目演进设计区（docs/ 下 00~12、ADR-002~006、IMPLEMENTATION_MATRIX） | **只读基线**，不写入新业务代码 |
| `D:\workplace\PyCharmMiscProject\prompt-API-key` | 无关项目（爬虫 Python、视频脚本等） | 忽略，不视为需求 |

约束原文：若当前目录不是新项目 checkout，不得在 `仓储` 中直接写新业务代码；`仓储` 只作为架构、测试和安全基线。

## 2. 运行环境快照（实测）

- Python 3.12.10 / pip 25.0.1 / pytest 9.1.1
- Node v24.19.0 / pnpm 11.23.0 / Docker 29.7.2 / git 2.55.0
- GitHub API 直连**失败**（基础连接关闭，疑似网络/代理受限）；`dianshang-shouhou` 仓库本机无法验证。`仓储/docs/README.md` 记载其"当前公开仓库为空，待初始化"。

## 3. 现状：已实现 / 实验中 / 规划中（严格区分）

### ✅ 已实现（有代码 + 测试证据，位于 `私域`）

| 项 | 证据 |
| --- | --- |
| `AGENTS.md` 工程宪法 v0.2（12 条） | 文件存在，自动代理工作规则 |
| `README.md` 设计基线（分层架构、信任边界、V1 闭环） | 文件存在，阶段标注为"设计基线" |
| 确定性退款领域服务 | `src/domain/models.py`（Role/RefundStatus/ErrorCode/强类型命令/RefundAction/AuditEvent）、`src/domain/idempotency.py`（同键同载荷幂等、异载荷拒绝）、`src/domain/refund_service.py`（权限/金额/状态机/审计，内存仓储） |
| 单元测试基线 | `tests/test_refund_service.py`：`python -m pytest tests/ -v` → **14 passed**（本会话实测） |

### 🧪 实验中

- 无。本仓库当前没有任何带实验编号、数据集、模型与指标结论的实验。

### 🟡 规划中（新项目未实现；部分有 `仓储/docs` 设计，状态均为"提议 / 未实现"）

| 模块 | 内容 | 依据 |
| --- | --- | --- |
| 阶段 0 完整化 | 配置管理、日志/脱敏、错误码扩展、数据库迁移骨架、CI、README/验收清单更新 | 用户阶段 0 定义 |
| 阶段 1 售后插件 | Tenant/Customer/Order/OrderItem/AfterSalesTicket/PolicyDocument/EvidenceChunk/Approval/Operation/AuditEvent；订单核验、资格、退款上限、状态机、幂等命令 | `仓储/docs/01_domain/domain-boundaries.md` |
| 阶段 2 单 Agent 闭环 | LangGraph 意图→澄清→只读检索→草稿→审批中断→恢复→审计 | `仓储/docs/02_agents/orchestration.md`、ADR-002（提议） |
| 阶段 3 工具与 RAG | 严格 JSON Schema 工具、TenantContext、权限拦截、关键词+向量检索、引用校验、注入防护 | `仓储/docs/03_tools/tool-contracts.md`、ADR-003（提议） |
| 阶段 4 可靠性与评测 | 重试/熔断/只读降级/人工接管/`operation_unknown`、黄金集、P50/P95、Token/成本、安全不变量 | `仓储/docs/06_reliability`、`08_evaluation`、ADR-006（提议） |
| 阶段 5 多 Agent/微调 | Supervisor+子 Agent、CrewAI 可选、Graphiti/Neo4j、LoRA/QLoRA | ADR-002/004/005（提议，未实现） |
| 阶段 6 Mule Agent Bridge | 外部 Agent 网络适配器（身份映射/租户注入/Schema 校验/超时/审计/断路） | `仓储/docs/03_tools`、ADR-003（提议） |

> 注意：`仓储`（StockMind V1）本身是另一个已完成系统（仓储补货域），仅作架构/测试/安全基线，其"已实现"不构成新项目的已实现。

## 4. 风险清单

| # | 风险 | 严重度 | 缓解 |
| --- | --- | --- | --- |
| R1 | 工作区无 git、远程仓库本机不可达（无 clone/无 push 能力） | 高 | 决策项 D1：建议本地 `git init` + 预置 remote（暂不推送）；推送待网络/凭据就绪 |
| R2 | 中文路径 + Windows 控制台代码页导致 pytest cache 写失败（实测 WinError 5 与路径乱码） | 中 | 脚本默认 `-p no:cacheprovider` 或 `--cache-clear`；CI 使用 UTF-8/英文路径 |
| R3 | `仓储/docs` 是"设计区"，容易被误当成实现证据 | 中 | 以本报告状态矩阵 + 实际测试输出为准；状态词严格受控 |
| R4 | 多 Agent 并行改动同一事实源文件 | 高 | `docs/TASK_SPLIT.md` 文件所有权矩阵 + 子代理禁改清单 + 总负责人审查 |
| R5 | 现有退款最小闭环与新售后插件并存造成的重复/混淆 | 中 | 保留现有文件不动；新插件独立目录；是否合并列为决策项 D2/D3 |
| R6 | 无 CI、无锁版本依赖（当前仅标准库 + pytest，风险低） | 低 | 测试基线任务卡 C 提供脚本与 CI 模板 |
| R7 | 合成数据被误读为真实收益 | 宪法 | 所有演示数据 = 固定随机种子合成数据，文档统一声明 |

## 5. 待用户决策项

- **D1**：是否在 `私域` 执行 `git init` 并添加 remote `https://github.com/fengyun-zpd/dianshang-shouhou`（仅本地提交，暂不推送）？
- **D2**：阶段 0/1 完成后，是否允许更新 `README.md`（追加实施状态导航与第一版验收清单）？宪法要求文档与实现同步，但 README 现有内容需保留为历史基线。
- **D3**：单 Agent 闭环（任务卡 B，阶段 2）的启动时机：阶段 1 验收后立即启动，还是等 README 回填后再启动？
- **D4**：网络/GitHub 访问方式：由用户修复后 clone 到独立目录开发，还是接受"私域本地开发 + 文档化提交记录、由用户自行推送"？

## 6. 修订记录

- v0.1（本会话）—— 首次侦察：确认工作区、Git 状态、运行环境、基线测试 14/14；梳理目录角色与状态矩阵；输出风险与决策清单。
