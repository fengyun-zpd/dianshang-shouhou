# 项目现状与风险报告

> 归属：电商售后多智能体工单系统（OpsPilot After-Sales，目标远程 `fengyun-zpd/dianshang-shouhou`）
> 性质：总负责人 Agent 的侦察与实施基线记录。本文档只陈述事实与判断，不把规划写成已实现；实时状态以此文件与测试输出为准。
> 版本：v0.7（阶段 0–6 全部完成，2026-09-04）

## 1. 工作区与目录角色

| 路径 | 角色 | 规则 |
| --- | --- | --- |
| `D:\workplace\PyCharmMiscProject\私域` | **新项目本地工作区**（OpsPilot 售后工单） | 允许写业务代码；**已是本地 git 仓库**（remote 预置，未推送） |
| `D:\workplace\PyCharmMiscProject\仓储` | StockMind V1 参考基线 + 新项目演进设计区 | **只读基线**，不写入新业务代码 |
| `D:\workplace\PyCharmMiscProject\prompt-API-key` | 无关项目（爬虫 Python 等） | 忽略，不视为需求 |

## 2. 运行环境与仓库（实测）

- Python 3.12.10（C 盘，仅解释器）＋ **D 盘虚拟环境 `.venv`**：`D:\workplace\PyCharmMiscProject\私域\.venv\Scripts\python.exe`（项目依赖一律装此，含 langgraph 1.2.11 / pytest 9.1.1）。
- **安装约定（用户直接指令）**：此后任何程序/依赖一律安装到 D 盘并汇报全部路径与最显眼文件。已执行：C 盘全局 langgraph 系列已卸载清理（site-packages 无残留），依赖迁至 D 盘 `.venv`；pip 缓存仍在 C 盘 `c:\users\zao'pei'de\appdata\local\pip\cache`（未迁移，如需可清）。
- GitHub 直连失败；用户全局配置 ghfast.top 代理镜像（`url.https://ghfast.top/https://github.com/.insteadof https://github.com/`）；PyPI 直连超时，安装使用清华镜像 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。
- 本地 git：`main` 分支；基线提交 `5877ce9`，阶段 0/1 提交 `aee6fbb`（未推送，D4 未决）。

## 3. 状态矩阵（已实现 / 实验中 / 规划中）

### ✅ 已实现（有代码 + 测试证据）

| 项 | 证据（`.venv` 下 `python -m pytest tests/` → **194 passed**） |
| --- | --- |
| 工程宪法 `AGENTS.md` v0.2；README 设计基线 + 实施进度节 | 文件存在 |
| 退款最小闭环（确定性领域服务） | `src/domain/{models,idempotency,refund_service}.py` + `tests/test_refund_service.py`（14 项） |
| 售后领域插件（阶段 1） | `src/domain/after_sales/`：实体/状态机/错误码、政策规则与冲突检测、确定性服务；测试覆盖正常/部分退款/关单守卫/缺参/订单核验/政策冲突/窗口/金额/权限矩阵/幂等/非法迁移/unknown 恢复；阶段 2 追加只读查询与确定性退款计划（`get_order_by_id` / `list_customer_tickets` / `compute_refund_plan`），共 40 项 |
| 平台层最小落地 | `src/platform/redact.py`（PII 脱敏）、`logging_config.py`（整行脱敏 formatter）；测试 8 项 |
| **阶段 2 单 Agent 闭环（LangGraph 1.2.11）** | `src/agents/`：状态 Schema（13 必含字段）、规则化意图识别、受控网关（写角色固定）、StateGraph 节点与条件路由、审批 interrupt/resume（草稿落库后挂起、恢复重读领域事实源）、高层运行器；测试 33 项（Schema/路由/澄清/转人工/审批拒绝/伪造审批/非法恢复/重复 resume 幂等/unknown/审计边界/金额注入防护）；演示脚本 `scripts/demo_interrupt_resume.py` 三条路径真实执行 |
| 测试工具链 | `scripts/run_tests.py`（分层回归，`REGRESSION PASS`）、`tests/conftest.py`（固定种子 42）、pytest.ini、`docs/TESTING_BASELINE.md` |
| CI 模板 | `.github/workflows/ci.yml`（云端激活后验证，属"已提交未在云端运行"） |
| **阶段 3 工具契约 + TenantContext + RAG** | `src/platform/tooling.py`（严格 JSON Schema 工具注册表：角色权限/防跨租户/入出参校验/超时/审计）；`src/rag/`（政策文档分块、关键词 + 可插拔向量（字典余弦）混合检索、引用校验、提示注入双向防护）；`src/agents/toolkit.py` 只读工具（get_order/get_ticket/list_customer_tickets/retrieve_policy）；测试 35 项 |
| **阶段 4 可靠性与评测** | `src/platform/reliability.py`（有限重试/熔断/只读降级/fail-closed/接管标记）；黄金集 `evals/golden/golden_v1.json` + 回放器 `evals/replay.py` → **11/11 通过**，报告 `evals/reports/golden_v1_report.md`（完成率/意图/引用/澄清率/P50-P95/安全不变量）；测试 13 项 |
| **阶段 5A 受控 LLM 运行时** | `src/models/`（config 白名单/Key 校验、base 协议+内容守卫、schemas 结构化输出、prompts 版本化、offline 规则适配器、openai_compatible HTTP 适配、router 能力矩阵+降级链）；影子评测 `evals/run_model_shadow_eval.py`（offline 实测 意图准确率 1.0；candidate 未配 Key → 安全降级未联网标注"未实测"）；`docs/MODEL_EVALUATION.md`；测试 32 项 |
| **阶段 5B Supervisor 多 Agent 实验** | `src/agents/subagents.py`（只读子 Agent 白名单：order/history/policy）、`supervisor.py`（SupervisorRunner，与单 Agent 同 API/状态/审批语义）、graph 支持 evidence 节点替换；对照实验 `evals/compare_agents.py` → 两模式均 11/11、outcome/退款 100% 一致 → 按 ADR-002 **默认维持单 Agent**、Supervisor 保留可选运行时；`docs/MULTI_AGENT_EXPERIMENT.md`；测试 11 项 |
| **阶段 6 Mule Agent Bridge** | `src/bridge/`（models 身份/动作白名单/入出站 Schema + bridge 适配器：身份映射、租户注入、超时、审计、熔断 fail-closed、注入拒绝；白名单仅只读查询 + submit_after_sales_request，无审批/执行）；`docs/MULE_BRIDGE.md`；测试 12 项；MCP/A2A 协议包装为规划 |
| 依赖清单 | `requirements.txt`（langgraph==1.2.11 / pytest==9.1.1 / httpx==0.28.1 / pydantic==2.13.5，安装到 D 盘 `.venv`） |
| 规划文档 | `docs/{STATUS_AND_RISKS,TASK_SPLIT,ARCHITECTURE,TESTING_BASELINE}.md` |

### 🧪 实验中

- 无带编号实验（无数据集/模型/指标结论）。

### 🟡 规划中（未实现，不写成已实现）

| 模块 | 内容 | 依据 |
| --- | --- | --- |
| 生产化工程 | PostgreSQL 唯一事实源 + alembic、真实网络端点部署、MuleSoft/MCP server 实接、真实 LLM Key 实测、LoRA/QLoRA/DPO（无对照数据禁止）、CrewAI（需可量化收益）、Graphiti/Neo4j、前端控制台 | ADR-002~006 |
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
| R8 | 子代理执行卡滞（无产出） | 中 | ✅ 已处理：中断两个卡滞子代理，由总负责人接管完成交付（阶段 0/1 记录） |
| R9 | 当前无 LLM 运行时，意图识别为规则实现 | 中 | 接口化：`src/agents/intent.py` 纯函数可替换为 LLM 适配器（阶段 3+ 前不引入） |

## 5. 决策项状态

- **D1** ✅ 已执行：`git init -b main` + remote origin（ghfast.top 代理前缀）+ 本地提交 `5877ce9`。
- **D2** ✅ 已批准：README 追加"实施进度"节（历史基线保留）。
- **D3** ✅ 已执行：用户于阶段 1 完成后直接下达任务卡 B（阶段 2）指令，单 Agent 闭环已实现（LangGraph 1.2.11，33 项测试）。
- **D4** ⏳ 未决：推送/远程同步方式（本地开发 + 文档化提交，由用户推送 / 或修复网络后由本 Agent 推送）。

## 6. 修订记录

- v0.1（2026-09-04）—— 首次侦察：工作区、Git、环境、基线测试 14/14；目录角色与状态矩阵；风险与决策清单。
- v0.2（2026-09-04）—— 阶段 0/1 完成：售后领域插件 + 平台脱敏日志 + 测试工具链落地，全量 53 passed；D1 执行、D2 批准；新增 R8 与验收证据。
- v0.3（2026-09-04）—— 阶段 2 完成：LangGraph 1.2.11 单 Agent 工作流（src/agents + 33 项测试 + 演示脚本），全量 95 passed；依赖迁至 D 盘 `.venv` 并记录安装约定；D3 执行；新增 R9。
- v0.4（2026-09-04）—— 阶段 3（工具契约/TenantContext/RAG）与阶段 4（可靠性/黄金集评测）完成：全量 139 passed；黄金集 golden-v1 11/11，任务完成率 1.0；新增任务卡 D/E 与评测证据。
- v0.5（2026-09-04）—— 阶段 5A（受控 LLM 运行时/能力矩阵/影子评测）完成：全量 171 passed；离线影子意图准确率 1.0；候选模型因未配置 Key 保持"未实测"（安全降级、未联网）；新增任务卡 F、docs/MODEL_EVALUATION.md。
- v0.6（2026-09-04）—— 阶段 5B（Supervisor 只读子 Agent 实验）完成：全量 182 passed；A/B 对照两模式均 11/11 → 默认维持单 Agent（ADR-002 回退条款），Supervisor 保留可选运行时；新增任务卡 G、docs/MULTI_AGENT_EXPERIMENT.md。
- v0.7（2026-09-04）—— 阶段 6（Mule Agent Bridge）完成：全量 194 passed；桥接层仅只读 + 发起请求（无审批/执行），身份/租户/Schema/超时/熔断/审计安全边界测试 12 项全过；新增任务卡 H、docs/MULE_BRIDGE.md。阶段 0–6 主线全部完成。
