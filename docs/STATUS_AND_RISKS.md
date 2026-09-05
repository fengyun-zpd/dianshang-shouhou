# 项目现状与风险报告

> 归属：电商售后多智能体工单系统（OpsPilot After-Sales，目标远程 `fengyun-zpd/dianshang-shouhou`）
> 性质：总负责人 Agent 的侦察与实施基线记录。本文档只陈述事实与判断，不把规划写成已实现；实时状态以此文件与测试输出为准。
> 版本：v0.17（阶段 0–6 + J/K1 + K2–K6 + 收口 + PG DB 约束闭环，2026-09-04）

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
- 本地 git：`main` 分支；项目以本地提交演进（最新提交见 `git log --oneline -1`），工作区与 HEAD 一致；**尚未推送**，D4 未决。

## 3. 状态矩阵（已实现 / 实验中 / 规划中）

### ✅ 已实现（有代码 + 测试证据）

| 项 | 证据（`.venv` 下 `python -m pytest tests/` → **417 passed, 0 xfailed**，2026-09-05 实测；PG 集成 33/33） |
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
| **阶段 6 Mule Agent Bridge** | `src/bridge/`（models 身份/动作白名单/入出站 Schema + bridge 适配器：身份映射、角色矩阵、租户注入、超时、审计、熔断 fail-closed、注入拒绝；白名单仅只读查询 + submit_after_sales_request，无审批/执行）；`docs/MULE_BRIDGE.md`；测试 14 项；MCP/A2A 协议包装为规划 |
| **可恢复持久化原型（SQLite）** | service 新增 `export_state/restore_state`（不动规则）+ `idempotency.export/import_records`；`src/persistence/`（codec JSON 安全编解码、SQLite append-only journal + checksum、`RecoverableSession` load/persist）；恢复保真/幂等续跑/损坏 fail-closed/跨库隔离测试 6 项；演示 `scripts/demo_persistence.py`；`docs/PERSISTENCE.md` |
| **任务卡 J 一致性与恢复安全** | 线程生命周期（请求指纹进 checkpoint：同 thread 只能继续原请求、结束线程不同请求拒绝、同请求重复提交返回原结果、禁止新 order 与旧 ticket 混合、租户绑定不可变）；快照严格校验（顶层键集合/引用关系/租户一致/金额有限且非负/refunded==已执行求和且≤实付/seq 单调/状态组合/审计与幂等引用，一律 SnapshotCorruptionError）；原子恢复（先验后换，失败原服务零改动；`restore_into`）；原子幂等（IdempotencyStore per-key 锁 + get-or-reserve/commit/release；create_ticket/create_refund 锁内 CAS，创建失败释放占位）；测试新增 26 项 |
| **任务卡 K1 订单级退款并发一致性** | 订单级锁 `(tenant_id, order_id)`：`create_refund`/`execute`/`reconcile`/`close_ticket` 在同一订单锁内完成 unknown 检查、剩余金额/容量校验、状态迁移、refunded 累计与审计；执行与对账成功均有原子容量校验（累计+本次≤实付），超额抛 `AMOUNT_EXCEEDS_REMAINING` 且不迁移不累计；幂等命中先于金额/unknown 守卫（同键重复返回原结果）；换键在 unknown 时拒 `OPERATION_UNKNOWN_CONFLICT`；锁顺序订单锁→幂等键锁；设计说明 `docs/TASK_K1_ORDER_REFUND_CONCURRENCY.md`；测试 7 项 |
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
| R9 | 真实 LLM endpoint 尚未实测 | 中 | 已有离线规则客户端与 OpenAI-compatible 适配、URL/Key 安全校验；候选模式未配置 Key 时安全降级，不声称真实模型指标 |

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
- v0.8（2026-09-04）—— 可恢复持久化原型（SQLite）完成：全量 200 passed；service 导出/恢复 + append-only journal + 恢复会话（保真/续跑/损坏拒绝测试 6 项 + 演示）；新增任务卡 I、docs/PERSISTENCE.md。生产路线：PostgreSQL/alembic（规划）。
- v0.9（2026-09-04）—— 安全与可靠性缺陷修复：RAG 文档按租户隔离并拒绝同键异内容，远程 LLM Base URL 收紧端口/主机边界，模型输入递归防注入，证据计入 token 预算，工具/桥接超时 fail-closed 并取消 future，桥接动作增加角色矩阵，checkpoint 使用租户命名空间；新增针对性回归测试。
- v0.10（2026-09-04）—— 增加 checkpoint 租户命名空间与跨运行器恢复回归，当前全量测试 209 passed。
- v0.11（2026-09-04）—— 任务卡 J（一致性与恢复安全）完成：全量 235 passed（新增 26 项）；线程请求指纹/冲突拒绝/重复返回原结果；快照严格校验 + 原子恢复；幂等 per-key CAS 并发单飞；收敛既有重复请求语义为“返回原结果”。
- v0.12（2026-09-04）—— Task K1（订单级退款并发一致性）完成：全量 242 passed（新增 7 项）；订单级锁 + 执行阶段原子容量校验修复同订单不同键并发超退（60+60>100）；unknown 对账竞争、换键拒绝、幂等优先语义回归覆盖。
- v0.13（2026-09-04）—— K2 评测与文档收敛（g10 契约同步 11/11、citation 指标可区分 0.6667、清理过时数字）。
- v0.14（2026-09-04）—— K3 快照类型严格 + 原子恢复；K4 PostgreSQL Repository/schema/Alembic（本地 PG 集成实测 6/6，行锁并发 60+60 恰一成功）；K5 FastAPI（认证/角色门禁/结构化错误，8 项）；K6（golden_v2 120/120、RAG Recall@K/MRR/引用/注入 1.0、tests/e2e|property|security 24 项）——全量 303 passed。
- v0.15（2026-09-04）—— 诚实边界收口：K7 审批工作台前端**未实现**（无浏览器测试环境，不声称完成；API 端点已齐备可支撑）；K8 边界=单 Agent 默认/Supervisor 只读子 Agent/Mule 本地契约均已交付且无真实授权连接，**未做任何微调**（无对照数据）；真实 LLM 未实测（未配置 Key，不联网）；PostgreSQL 本地容器**真实实测**、非生产部署。
- v0.16（2026-09-04）—— 事实与文档统一收口：以真实命令输出为准统一全站基线（`pytest tests/ -q` = **303 passed**，PostgreSQL 容器运行时的集成 6/6 实测，无 PG 自动跳过；README/STATUS/ARCHITECTURE/TASK_SPLIT/PERSISTENCE/MULE_BRIDGE 等“当前”性数字与状态已更新，历史修订保留）；修复 `src/__init__.py`、`src/platform/__init__.py` 过时的“规划中”描述为已实现清单 + 未实现项。前端/真实 LLM/真实 MCP-MuleSoft/微调仍明确标注未实现或未实测；SQLite 仅恢复原型。
- v0.17（2026-09-04）—— PostgreSQL 事实源闭环：schema/Alembic 0002 增加 DB 级 CHECK（金额非负/退款金额为正/工单与操作状态枚举），数据库可独立阻止非法业务状态；集成测试 9 项（新增 DB 约束拦截与并发同幂等键恰一成功）；全量 **306 passed**。
- v0.18（2026-09-04）—— 收敛第三~四阶段：FastAPI 安全审查补充（请求体 tenant 不信任、重复点击审批/对账幂等化 409、审计响应无 PII，API 测试 12 项）；RAG 检索排序平局按 chunk_id 稳定化（消除进程间不确定，指标稳定：recall@1=0.23/recall@3=0.80/MRR=0.4583/注入拒绝 1.0）；当前全量 **310 passed**（PG 集成 9/9）。
- v0.19（2026-09-04）—— 第二轮事实与文档收口（全部以本次真实运行输出为准，本机 PostgreSQL 容器运行中）：`pytest tests/ -q` = **310 passed**（PG 集成 9/9 实测，0 skip）；黄金集回放 v1 = 11/11、v2 = 120/120；Agent/Supervisor 对照 11/11 vs 11/11（outcome/退款 100% 一致，维持单 Agent）；离线影子意图准确率 1.0。文档更新：`docs/TESTING_BASELINE.md` 分层状态由"规划中"实化为 unit 269 / integration 9 / e2e 2 / property 12 / security 4 / 根 14 = 310；README/ARCHITECTURE/TASK_SPLIT/PERSISTENCE/MULE_BRIDGE 中"当前"指针统一为 310 passed 与集成 9/9（历史修订行保留）；PG 边界措辞改为"Repository/schema/Alembic 已实现并本地实测，领域状态机整体 SQL 化未实现"。说明：任务输入基线"297 passed, 6 skipped"为本机 PG 离线旧快照，与本轮容器运行中的真实输出 **310 passed、0 skip** 不一致，已按宪法第二条以真实运行与代码为准，未写入文档。
- v0.20（2026-09-04）—— 阶段二 PostgreSQL 唯一事实源第一步（命令级原子写地基）：Repository 增加 `unit_of_work` 事务作用域（interfaces + memory 快照回滚 + PG thread-local 共享连接/事务；正常提交、异常整单位回滚=无部分提交、禁嵌套）；全部读写方法接入同一连接上下文（跨表同事务）。契约测试 +4（unit 273）、PG 集成 +4（integration 13，真实 PG 实测跨表原子提交/中途异常零残留/作用域内读自身写/嵌套拒绝）；全量 **318 passed**。文档同步：POSTGRES v1.2、TESTING_BASELINE v0.3、README/ARCHITECTURE/TASK_SPLIT/PERSISTENCE/MULE_BRIDGE 当前指针统一 318/13/13。诚实边界：领域状态机整体 SQL 化与 PG-backed 领域服务运行仍未实现（本能力是其前置地基），checkpoint 仍仅存流程状态。
- v0.21（2026-09-04）—— 阶段二 PG-backed 领域会话实现（事实确在 PostgreSQL 行表并可装载重建）：`src/persistence/pg_backed.py`（`PgBackedSession`：`unit_of_work` 内整库镜像写/异常整单位回滚；`load()` 从行表装配全新领域服务，同幂等键同载荷在重建实例上仍返回原结果=重启不重复副作用，审计在重建实例上继续追加）；Alembic 0003（tickets 增 created_by/reason_tags，已在本机 PG 实测 upgrade head→0003）；Repository 增加恢复装载方法 `list_orders/tickets/operations/audit/idem` 与 `clear_all`；保真 codec（Order/Ticket 含 reason_tags·created_by/Operation 含 decision_version·executed/审计/幂等；订单明细 items 与政策不入表——如实声明）。测试：unit codec 7 项、PG 集成 live 4 项（SQL 直接断言事实在 PG/roundtrip/save 失败整单位回滚先落镜像不被覆盖/重启续跑无重复副作用）；演示 `scripts/demo_pg_backed.py`（落 PG→重建→重复返回原工单→继续关单审计 5→7）。API 认证身份抽象为 `TokenResolver` 可替换端口（内存 `ApiTokenRegistry` 仅测试实现，中间件只依赖协议）+1 测试。全量 **330 passed**（unit 281 / integration 17 / e2e 2 / property 12 / security 4 / 根 14）。文档同步：POSTGRES v1.3、TESTING_BASELINE v0.4、README/ARCHITECTURE/TASK_SPLIT/PERSISTENCE/MULE_BRIDGE 指针统一 330/17/17。诚实边界：领域命令仍内存裁决（PG-backed 为单实例命令后全量镜像写），跨进程并发一致性与增量 SQL 化编排未实现；checkpoint 仅存流程状态、与 DB 无覆盖关系。
- v0.22（2026-09-04）—— 阶段三可持久化工作流 checkpoint：`src/agents/checkpoint.py`（`open_sqlite_checkpointer`：SqliteSaver 落 SQLite 文件；checkpoint 只存流程恢复状态，业务事实一律重读领域服务/PG，禁止从 checkpoint 恢复金额/状态/审批/执行）；依赖 `langgraph-checkpoint-sqlite==3.1.1`（requirements.txt 记录，清华源装至 D 盘 .venv）。测试：单测 +3（unit 284——同文件新运行器"重启" resume 正确完成不重放、注入伪造 outcome 视图不能覆盖领域事实（仍 pending_approval/零退款/无 execute 审计）、checkpoint 真实落盘可被新连接读取）；PG 集成 +1（integration 18——挂起审批→业务事实 save 落 PG→重启后领域服务从 PG 重建 + 同一持久 checkpoint resume→审批/执行正确、工单/操作仅各一（无重放）、SQL 断言 refund_operations executed）；全量 **334 passed**（PG 集成 18/18）。文档：TESTING_BASELINE v0.5、README/ARCHITECTURE/TASK_SPLIT/PERSISTENCE/MULE_BRIDGE 指针统一 334/18/18。诚实边界不变：命令仍内存裁决、增量 SQL 化编排未实现；checkpoint 仅流程状态（本层与 PgBackedSession 组合验证"重启续跑不重复副作用"）。
- v0.23（2026-09-04）—— 阶段六观测/CI 与最终收口复跑：`.github/workflows/ci.yml` 模板补全（requirements 完整安装 + PostgreSQL 服务注入 + alembic upgrade head + 五层回归；注明模板未在本仓库真实运行、无 PG 时集成自动 skip）；最终六命令全部真实复跑通过：`pytest tests/ -q` = **334 passed**、`run_tests.py` = REGRESSION PASS、`replay.py --dataset golden_v2` = **120/120**、`compare_agents.py` = **11/11 vs 11/11**（维持单 Agent）、`run_model_shadow_eval.py --mode offline` = **意图 1.0**、`git diff --check` = 0；RAG 指标复跑可复现：recall@1=0.23 / recall@3=0.80 / MRR=0.4583 / 注入拒绝 1.0。评测/对照产物（golden_v2_report/agent_compare/shadow_eval_offline）以本次运行为准。
- v0.24（2026-09-04）—— 升级目标第一阶段：缺陷台账。新增 `tests/regression/test_defect_ledger_regressions.py` 与 `docs/DEFECTS_LOG.md`：先补 12 项回归并执行（`4 passed, 7 xfailed`，PG 容器运行中）——通过保障：D3 客户只读自己工单、D5 审批版本 CAS（顺序）、D10 unknown 仅原 operation_id、D11 外部未知不换键、D6 之外的 PG 能力。**确认 7 项缺陷（xfail 记录，修复后转 PASS）**：D1 跨租户同 order_id 领域索引覆盖（P0）、D2 幂等裸键跨租户冲突（P0）、D4 RejectCommand 无 expected_version（P1）、D5 并发审批缺 op 级锁/DB CAS（P1）、D6 with_order_lock 读后即 commit 未在业务期持锁（P0，实测 contender 未被阻塞 dt≈0.03s）、D7 save 失败内存/DB 分叉（P0，全量 clear/reinsert 路径）、D8 政策与订单明细不入表重启不能完整恢复（P1）、D12 对照报告含逐 run 耗时致非确定 diff（P1）。全量 **338 passed, 7 xfailed**（regression 层 +11=4pass/7xfail）。修复归属：第二阶段（D1/D2/D4/D5/D6/D7/D8）、第四阶段（D9 workflow_threads+租约）、第七阶段（D12 报告稳定化）。
- v0.25（2026-09-04）—— 第二阶段修复第一批（D4/D6，xfailed 7→5，全量 **340 passed, 5 xfailed**）：**D4** `RejectCommand` 增加 `decision_version`（models/service.reject 校验 + 拒绝推进 version，与 approve 对称防并发竞争；schemas `DecisionIn.expected_version`、API approve/reject 路由读取、`ports.submit_approver_decision` reject 分支传入当前版本）；**D6** `PostgresAfterSalesRepository.with_order_lock` 改为事务内 FOR UPDATE 保持至 yield 体业务完成后再 commit/rollback（此前读后即提交、业务期无锁）；对应 XFAIL 转 PASS（regression `6 passed, 5 xfailed`）。诚实边界：D1/D2（领域复合索引/幂等租户作用域）、D7（废弃 clear/reinsert）、D8（政策/明细入表）随 PG-first 命令服务推进（第三阶段）修复；D5 并发审批 CAS 同；D12 第七阶段报告稳定化。
- v0.26（2026-09-04）—— 第二阶段修复第二批（D2，xfailed 5→4，全量 **341 passed, 4 xfailed**）：幂等键租户作用域——`service.create_ticket/create_refund` 命令内幂等键统一为 `f"{tenant}:{key}"` 租户前缀规范键（store per-key 锁、占位/提交/释放、审计字段、Operation.idempotency_key 同值一致；快照导出/validate/pg_backed 形状不变）。验证：跨租户同原始 key 各自成功（不再全局冲突）、同租户同 key 幂等语义保留（返回原对象不重复建单）；对应 XFAIL 转 PASS（regression `7 passed, 4 xfailed`）。剩余 XFAIL：D1（领域订单裸键/跨租户同 order_id，需导出结构演进或随 PG-first 收编）、D7（废弃全量 clear/reinsert）、D8（政策/明细入表）、D12（报告稳定化）。
- v0.27（2026-09-04）—— 第二阶段修复第四批（D1，xfailed 4→3，全量 **342 passed, 3 xfailed**）：内存后端订单 seed 跨租户同 order_id 显式拒绝（fail-closed，不再静默覆盖；多租户同 order_id 共存权威语义由 PostgreSQL `(tenant_id, order_id)` 主键承载，repository 层已实测）；对应 XFAIL 转 PASS（regression `8 passed, 3 xfailed`）。剩余 XFAIL：D7（废弃全量 clear/reinsert + 失败回滚重读）、D8（政策/明细持久化）——两者随第三阶段 PG-first 命令服务收编；D12（报告稳定化）第七阶段。第二阶段内存层修复完成（D1/D2/D4/D6 + D3/D5/D10/D11 通过项）；剩余生产语义（单事务命令、失败回滚重读）进入第三阶段 PG-first。
- v0.28（2026-09-04）—— 第三阶段设计基线：新增 `docs/PG_FIRST_SERVICE.md`（未实现，不写成已实现）——PG-first 命令服务完整设计：规则纯化（rules.py 单一事实源防漂移）、数据面扩展（Alembic 0004：policies/order_items/entity_seq，收编 D8）、Repository 命令面（迁移 CAS/next_seq/幂等唯一）、八命令单事务模板（读最新事实→租户/版本条件→订单行锁容量→业务状态/幂等/审计同事务→提交后返回、失败稳定错误码）、测试矩阵（unit rules/contract/integration 无部分提交与并发/e2e；D7·D8 XFAIL 收编点）与里程碑切分（六步逐命令落地）。诚实声明：本会话上下文已近极限，第三阶段实现（规则抽取+schema 0004+逐命令单事务）需在充足上下文的会话中执行；交接面 = 目标 goal-6d447b17 + 本文件 + docs/DEFECTS_LOG.md + 基线提交 67f1687（342 passed / 3 xfailed）。
- v0.29（2026-09-04）—— 阶段三实施第一步（D3 客户级资源授权补强，RED→GREEN）：新增 API 回归（同租户 C1/C2 各建工单后，C1 token 读 C2 工单 → 403 `AFTER_SALES_PERMISSION_DENIED` 且响应不含 C2 订单号/原因/电话；自己工单 200；反向亦然）；修复 `GET /tickets/{ticket_id}`——CUSTOMER 角色增加 `customer_id` 资源级校验（此前仅租户门禁，同租户可越权读他人）。API 测试 +2（15 passed）；全量 **344 passed, 3 xfailed**（PG 18/18）。DEFECTS_LOG D3 更新：原 `test_customer_only_own_ticket` 仅覆盖创建侧，已补充读取侧资源授权证据。
- v0.30（2026-09-04）—— 阶段三第二步（规则抽取，PG_FIRST_SERVICE 里程碑 1）：新增 `src/domain/after_sales/rules.py` 确定性规则纯函数（无副作用/可单测/稳定错误码/不依赖 LLM/不允许模型覆盖：角色权限、订单租户·客户匹配、金额容量、表驱动状态机、expected_version CAS、幂等命中、operation_unknown 守卫、关单定性；客户-订单匹配为新增规则）；接入 service（approve/reject 版本校验与 `_apply_op/_ticket_transition` 状态迁移改用 rules，错误码与消息文本保持）；9 项 rules 单测；全量 **353 passed, 3 xfailed**（PG 18/18）。
- v0.31（2026-09-04）—— 阶段三第三步（Alembic 0004 数据模型）：新增 `policies`（含 version/effective_from，`(tenant, policy_id, version)` 主键与 ratio/window CHECK、多版本共存）、`order_items`（`(tenant, order, sku)` PK + FK + quantity/price CHECK）、`entity_seq`（租户作用域自增 `(tenant, kind)`，kind CHECK）至 schema.sql 与迁移 0003→0004（本机 PG 实测 version=0004，9 表就位）；更新三处集成 fixture 清表列表；新增 `tests/integration/test_schema_0004_live.py` 4 项（表存在/政策版本唯一与 CHECK/明细 FK·CHECK/序列递增）；全量 **357 passed, 3 xfailed**（unit 295 / integration 22 / e2e 2 / property 12 / security 4 / 根 14 / regression 8+3xfail）。政策/明细的 Repository 装载与恢复（D8 收编）在第四步。
- v0.32（2026-09-04）—— 阶段三第四步前半（Repository 命令数据面方法）：interfaces/memory/postgres 新增 `PolicyRow`/`OrderItemRow` 与 `insert_policy`/`list_policies`/`insert_order_item`/`list_order_items`/`next_seq`（租户作用域原子递增：PG 用 `INSERT … ON CONFLICT DO UPDATE … RETURNING`，跨连接安全）；包导出同步。测试：repo 契约 +2（unit/memory：政策版本唯一与多版本/跨租户共存、明细与 next_seq 递增隔离）、PG live +2（政策/明细真实往返；8 连接并发取号恰 1..8 不重复、租户隔离）；全量 **361 passed, 3 xfailed**（unit 297 / integration 24）。政策/明细的 pg_backed 装载恢复（D8 转 PASS）下一步。
- v0.33（2026-09-04）—— 阶段三 D8 收编（政策/订单明细完整持久化与装载）：`pg_backed.py` save 现把政策（policy_to_row：reason_tags JSON、effective_from 用占位常量 `_POLICY_EPOCH`=1970-01-01，V1 政策无生效日期语义、第八阶段演进）与订单明细（order_item_to_row）落 policies/order_items 表；`load()` 无参默认自 DB 恢复政策与明细（不再要求调用方重新注入；显式传参兼容旧调用）；`clear_all` 删除序修正（order_items 先于 orders，规避 FK）；既有 codec/live 注释更新。测试：regression D8 去 xfail 转 PASS（`9 passed, 2 xfailed`）、pg_backed live +1（PG 无参恢复实证：SQL 断言 policies/order_items 行存在）；全量 **363 passed, 2 xfailed**（unit 299 / integration 25）。剩余 XFAIL：D7（废弃全量 clear/reinsert——随 PG-first 命令服务落地）、D12（报告稳定化）。
- v0.34（2026-09-04）—— 阶段三第四步版本迁移 CAS 原语补齐：Repository 增加 `update_ticket_versioned(row, expected_version)`（工单迁移 CAS：status/resolution+version+1）；`update_operation_versioned` 扩展为以 row 的 status/executed/decision_version 为目标、WHERE version=expected 且 version+1（approve/reject/execute/reconcile 各写路径的 CAS 语义）。契约 +2（工单迁移 CAS 冲突/操作迁移携带 decision_version·executed）、PG live +2（真实 PG 同语义 + 并发/过期版本冲突）；全量 **367 passed, 2 xfailed**（unit 301 / integration 27）。Repository 命令编排原语已齐（行方法 + unit_of_work + 行锁 + CAS + 幂等唯一 + 审计 + 政策/明细 + 序列），下一步第五步 PG-first 命令服务在其上编排八命令。
- v0.35（2026-09-04）—— 阶段三第五步（PG-first 命令服务，增量子集）：新增 `src/domain/after_sales/pg_commands.py` `PgCommandService`——单事务命令模板落地（create_ticket/submit/approve/reject 四个命令：unit_of_work 内读行→rules 校验（权限/客户-订单/版本 CAS/状态机）→订单行锁+政策命中+next_seq→业务行+幂等+追加审计→提交后重读返回领域对象；异常整事务回滚）。幂等规范键/载荷 hash 与 D2/内存一致；政策从 policies 表装载（policy_from_row）供匹配。9 项单测（memory repo 后端：建单事务提交/重复同键幂等/异载荷拒绝/CUSTOMER 他人订单拒绝/无政策转人工/submit/approve CAS 与过期版本拒绝/失败零残留审计/拒绝路径与角色门禁）；全量 **376 passed, 2 xfailed**（unit 310）。剩余四命令（create_refund_draft/execute/reconcile/close_ticket）按同模板补齐中；PG live 集成与默认运行时切换（工作流/API/gateway）待命令全集后接续。
- v0.36（2026-09-04）—— 阶段三第五步（PG-first 命令服务八命令全集）：补齐 create_refund_draft（订单行锁+草稿阶段容量校验+unknown 守卫+幂等）/execute（approved→executed 容量 CAS；timeout→unknown 不累计）/reconcile（unknown 仅原 op：success→executed 容量/failed）/close_ticket（订单行锁+无未决操作+依结果定性 resolve/reject→close 双版本 CAS）；Memory repo `update_operation_versioned` 补 executed 累计维护（容量语义与 PG 一致）；单测 +6（15 项全绿：草稿幂等与阶段容量/unknown 守卫/执行容量与 timeout/原键对账与终态拒绝/关单路径/未决操作拦截与定性）；全量 **382 passed, 2 xfailed**（unit 316）。八命令单事务路径（memory 后端验证）完成；PG live 集成（无部分提交/两连接并发 CAS/八命令 e2e）、默认运行时切换与 D7 收编为后续收口。
- v0.37（2026-09-04）—— 阶段三第五步收口（PG live + D7）：命令 CAS 冲突统一转译稳定错误码（`_op_cas/_ticket_cas` 捕获 `OptimisticLockError` → `DECISION_VERSION_MISMATCH`）；新增 `tests/integration/test_pg_commands_live.py` 4 项（真实 PG）：八命令单事务链 SQL 断言（tickets/refund_operations executed·decision_version/idem 两键/audit≥7）、审批过期版本失败整事务回滚零残留（approve 审计 0、submit 审计保留）、**两连接并发 approve 恰一成功**（另一转 DECISION_VERSION_MISMATCH）、**同订单两已批准 60 并发 execute 恰一成功**（另一 AMOUNT_EXCEEDS，累计=60、executed=1 行）。regression D7 去 xfail 转 PASS（生产命令路径无 clear_all 源码断言 + PgBackedSession 镜像工具标注；命令失败无部分提交由 live 实证）；全量 **387 passed, 1 xfailed**（唯一剩余 D12=报告耗时稳定化，归第七阶段）。
- v0.38（2026-09-04）—— 缺陷台账清零（D12 收编）：`compare_agents.py` 报告稳定化——移除 P50/P95 与逐 case 耗时列（报告仅确定性指标：outcome/退款/通过率），结论仅由通过率决定（不再依赖逐 run 耗时）；报告尾注声明"确定性产物、耗时仅 stdout"；两次运行报告哈希相同（零 diff）。regression **11 passed, 0 xfailed**（D1–D12 全部转 PASS）；全量 **388 passed, 0 xfailed**（unit 317 / integration 32 / e2e 2 / property 12 / security 4 / 根 14 / regression 11）。
- v0.39（2026-09-04）—— 阶段三第七步（API 可启动服务，最小增量）：`create_app` 增加可选 `pg_probe` 与公开端点 `/health/live`（恒 200）与 `/health/ready`（配置 pg_probe 时反映 PostgreSQL 可用性：不可用 → 503 degraded；未配置 → 200 not-configured）；`AuthMiddleware` 放行 `/health` 前缀（探活免认证）；新增 `scripts/run_api.py` 可启动入口（memory 开发后端 + 固定合成 seed + 演示 token；可选 `--pg-url` 仅启用 PG 探测；**不连接任何真实外部系统**，docstring 注明 PG-first 命令路由接入 API 为规划未实现）；health 单测 5 项；全量 **393 passed, 0 xfailed**（unit 322）。
- v0.40（2026-09-04）—— 阶段四（SQLite checkpoint 加固，最小增量）：`open_sqlite_checkpointer` 增加 WAL 日志模式（并发读写不互锁）与 `busy_timeout=5000ms`（多进程写竞争等待而非立即失败）；新增 `close_sqlite_checkpointer` 显式关闭（幂等，进程退出亦自动关闭）；docstring 明确损坏语义（文件损坏 → sqlite3.DatabaseError fail-fast，不得静默使用损坏流程状态）。测试 +2（5 项：WAL/busy_timeout PRAGMA 值断言；损坏文件（关闭+丢伴生文件+破坏头）原生读取抛 DatabaseError）；workflow thread 租户归属由既有 tenant 命名空间测试保障。全量 **395 passed, 0 xfailed**（unit 324）。
- v0.41（2026-09-04）—— 第八步文档统一收口（全站"当前性"数字对齐实测）：全量 **395 passed, 0 xfailed**；分层收集 unit 321 / integration 31 / e2e 2 / property 12 / security 4 / regression 11（0 xfailed）/ 根 14 = 395；PG 集成 31/31。README §13.1、ARCHITECTURE 启动行、TESTING_BASELINE v0.7（分层表加入 regression 层）、PERSISTENCE、MULE_BRIDGE、POSTGRES、TASK_SPLIT"当前全量"指针统一为 395/31/31；缺陷台账 D1–D12 全部标记已修复。历史修订行保留（含交付时数字）。未实现/未实测声明（如实）：真实 LLM、真实 MuleSoft/MCP 网络连接、微调、审批工作台前端、领域状态机增量 SQL 化之外的"默认运行时整体切换（gateway/API/工作流指向 PgCommandService）"、workflow_threads 表与 resume 跨进程租约、API 限流/CORS/OpenAPI 示例为规划中。

- v0.42（2026-09-05）—— 阶段四（PG-first 运行时闭环）进展：RED 契约 7/7 转 PASS（tests/phase4）——approve/reject 落 approval_decisions 事实（decided_by=principal 或 actor 兜底、decided_version、decision；同版本唯一约束由 0005 保证）、幂等三元组 (tenant, command_type, raw_key) 使不同 command_type 可同原始 key、政策仅用业务时间下最新有效版本（未来版本 POLICY_NOT_FOUND）、create_ticket customer 以订单事实为准无条件校验；Alembic 0005（approval 唯一/幂等三元组/workflow_threads 表）；全量 402 passed 0 xfailed。
- v0.43（2026-09-05）—— 阶段四第 3 节（runtime profile 门禁骨架）：src/api/runtime.py（RuntimeProfile memory|pg；pg 要求 PostgreSQL 可达 + alembic schema=0005 + 健康探测，不满足 RuntimeError **绝不静默降级内存**；require_postgres_ready/build_pg_command_service）；run_api.py 增 --require-pg 门禁（失败退出码 1）；单测 4 项（profile 值/可达且 0005 ok/不可达 raise/版本不符 raise）；全量 406 passed 0 xfailed。API/Gateway/Runner 的 AfterSalesApplicationPort 化与 PG profile HTTP e2e 为进行中（不声称已切换）。
- v0.44（2026-09-05）—— 阶段四第 5 节（测试隔离）：scripts/run_pg_tests_isolated.ps1 —— 每 worker 独立 database（opspilot_p4a/b，同实例）各自从空库 Alembic 全程升级（0001→0005 成功）后运行同一批 PG 集成（各 25 passed，exit 0）＝对象级完全隔离，并行亦互不踩库；实测 ISOLATED DOUBLE-RUN PASS。
- v0.45（2026-09-05）—— 阶段四收口进展：实测新基线 **417 passed, 0 xfailed**（unit 331 / integration 33 / e2e 2 / property 12 / security 4 / regression 11 / phase4 10 / 根 14）；run_tests.py 分层扩展 regression/phase4 独立统计；测试隔离：每 worker 独立 database（scripts/run_pg_tests_isolated.ps1）空库 Alembic 0001→0005 全程升级成功且各跑 PG 集成 25 passed（ISOLATED DOUBLE-RUN PASS）。未完成/进行中（如实）：WorkflowRunner/AfterSalesGateway 端口收敛与 PG adapter 工作流只读能力、Runner 内部强制租约（D9 收口项，repo 原语已有并测）、run_api --backend pg 真实装配、API 审批以认证 principal 为 decided_by、并行两进程集成运行。