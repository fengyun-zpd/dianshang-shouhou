# 项目现状与风险报告

> 归属：电商售后多智能体工单系统（OpsPilot After-Sales，目标远程 `fengyun-zpd/dianshang-shouhou`）
> 性质：总负责人 Agent 的侦察与实施基线记录。本文档只陈述事实与判断，不把规划写成已实现；实时状态以此文件与测试输出为准。
> 版本：v0.2（阶段 0 完成 / 阶段 1 完成，2026-09-04）

## 1. 工作区与目录角色

| 路径 | 角色 | 规则 |
| --- | --- | --- |
| `D:\workplace\PyCharmMiscProject\私域` | **新项目本地工作区**（OpsPilot 售后工单） | 允许写业务代码；**已是本地 git 仓库**（remote 预置，未推送） |
| `D:\workplace\PyCharmMiscProject\仓储` | StockMind V1 参考基线 + 新项目演进设计区 | **只读基线**，不写入新业务代码 |
| `D:\workplace\PyCharmMiscProject\prompt-API-key` | 无关项目（爬虫 Python 等） | 忽略，不视为需求 |

## 2. 运行环境与仓库（实测）

- Python 3.12.10 / pip 25.0.1 / pytest 9.1.1 / git 2.55.0；Node/Docker 可用。
- GitHub 直连失败；用户全局配置 `url.https://ghfast.top/https://github.com/.insteadof https://github.com/`（代理镜像）。`git remote -v` 显示 origin 已带 ghfast.top 前缀。远程仓库本机未验证内容（设计文档记载其"待初始化"）。
- 本地 git：`main` 分支，基线提交 `5877ce9`（决策项 D1 已执行，未推送）。

## 3. 状态矩阵（已实现 / 实验中 / 规划中）

### ✅ 已实现（有代码 + 测试证据）

| 项 | 证据（`python -m pytest tests/` → **53 passed**） |
| --- | --- |
| 工程宪法 `AGENTS.md` v0.2；README 设计基线 + 实施进度节 | 文件存在 |
| 退款最小闭环（确定性领域服务） | `src/domain/{models,idempotency,refund_service}.py` + `tests/test_refund_service.py`（14 项） |
| 售后领域插件（阶段 1） | `src/domain/after_sales/`：实体/状态机/错误码（models.py）、政策规则与冲突检测（policies.py）、确定性服务（service.py：订单核验、资格、退款上限、工单/操作状态机、幂等、operation_unknown 对账、审计）；测试 31 项覆盖正常/部分退款/关单守卫/缺参/订单核验/政策冲突/窗口/金额/权限矩阵/幂等/非法迁移/unknown 恢复 |
| 平台层最小落地 | `src/platform/redact.py`（手机/邮箱/身份证脱敏）、`logging_config.py`（整行脱敏 formatter）；测试 8 项 |
| 测试工具链 | `scripts/run_tests.py`（分层回归，`REGRESSION PASS`）、`tests/conftest.py`（固定种子 42）、pytest.ini（markers + `-p no:cacheprovider`）、`docs/TESTING_BASELINE.md` |
| CI 模板 | `.github/workflows/ci.yml`（云端激活后验证，属"已提交未在云端运行"） |
| 规划文档 | `docs/{STATUS_AND_RISKS,TASK_SPLIT,ARCHITECTURE,TESTING_BASELINE}.md` |

### 🧪 实验中

- 无带编号实验（无数据集/模型/指标结论）。

### 🟡 规划中（未实现，不写成已实现）

| 模块 | 内容 | 依据 |
| --- | --- | --- |
| 阶段 2 单 Agent 闭环 | LangGraph 意图→澄清→只读检索→草稿→审批中断→恢复→审计；任务卡 B（待 D3 确认启动） | `仓储/docs/02_agents`、ADR-002 |
| 阶段 3 工具与 RAG | JSON Schema 工具、TenantContext、注入防护、检索引用 | `仓储/docs/03_tools`、ADR-003 |
| 阶段 4 可靠性与评测 | 黄金集回放、P50/P95、Token/成本、安全不变量报告 | `仓储/docs/06_reliability`、`08_evaluation`、ADR-006 |
| 阶段 5 多 Agent/微调 | Supervisor+子 Agent、CrewAI 可选、Graphiti/Neo4j、LoRA 对照 | ADR-002/004/005 |
| 阶段 6 Mule Agent Bridge | 外部 Agent 网络适配器 | ADR-003 |
| 数据库迁移 | PostgreSQL 唯一事实源 + alembic（当前内存仓储） | 阶段 0 定义中该项以"可插拔接口 + 文档"落地，待引入 PostgreSQL 时实现 |

## 4. 风险清单（v0.2 更新）

| # | 风险 | 严重度 | 状态 / 缓解 |
| --- | --- | --- | --- |
| R1 | 无 git / 远程不可达 | 高 | ✅ 已缓解：本地 git init + remote 预置（D1）；推送待网络/凭据就绪（D4 未决） |
| R2 | 中文路径 pytest cache 写失败 | 中 | ✅ 已缓解：pytest.ini addopts 与 scripts 均 `-p no:cacheprovider` |
| R3 | `仓储/docs` 设计区被误当实现证据 | 中 | 以本文档状态矩阵与测试输出为准 |
| R4 | 多 Agent 并行改同一事实源 | 高 | 所有权矩阵 + 禁改清单 + 总负责人审查 |
| R5 | 退款最小闭环与售后插件并存 | 中 | 独立目录互不修改；合并与否待用户 |
| R6 | 无 CI | 低 | ✅ 模板已建；云端运行待仓库推送后验证 |
| R7 | 合成数据被误读为真实收益 | 宪法 | 统一声明固定种子合成数据 |
| R8 | 子代理执行卡滞（无产出） | 中 | ✅ 已处理：中断两个卡滞子代理，由总负责人接管完成交付（本轮记录） |

## 5. 决策项状态

- **D1** ✅ 已执行：`git init -b main` + remote origin（ghfast.top 代理前缀）+ 本地提交 `5877ce9`。
- **D2** ✅ 已批准：README 追加"实施进度"节（历史基线保留）。
- **D3** ⏳ 未决：任务卡 B（单 Agent 闭环）启动时机。
- **D4** ⏳ 未决：推送/远程同步方式（本地开发 + 文档化提交，由用户推送 / 或修复网络后由本 Agent 推送）。

## 6. 修订记录

- v0.1（2026-09-04）—— 首次侦察：工作区、Git、环境、基线测试 14/14；目录角色与状态矩阵；风险与决策清单。
- v0.2（2026-09-04）—— 阶段 0/1 完成：售后领域插件 + 平台脱敏日志 + 测试工具链落地，全量 53 passed；D1 执行、D2 批准；新增 R8 与验收证据。
