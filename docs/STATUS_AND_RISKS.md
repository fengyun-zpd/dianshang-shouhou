# 项目状态与风险

> 更新：2026-09-07（V1.1）。

| 项目 | 状态 | 事实边界 |
| --- | --- | --- |
| 单 Agent 售后闭环 | 已实现 | LangGraph 澄清、证据、审批恢复、unknown 对账 |
| 确定性领域服务 | 已实现 | `after_sales/` 与 PG 命令路径裁决金额、资格、状态、幂等、并发、审计 |
| 确定性证据检索基线（本地 RAG） | 已实现 | PolicyStore 检索：citation、版本/租户/启用约束、注入拒绝、无证据转人工；不裁决金额 |
| PostgreSQL profile | 已实现 | 命令事务、审批事实、租约和 API 装配 |
| 面试演示 | 已实现 | demo_interview（七场景）+ demo_rag_policy（检索展示）真实断言 |
| Supervisor/多 Agent 编排 | V1.1 实验 | 当前演示为四角色只读编排（Triage/Evidence/Resolution/RiskReview）；历史三角色实现仅作兼容对照；同黄金集 A/B 无收益，默认单 Agent；不是真实 LLM 子 Agent |
| 受控 LLM 适配（离线基线） | 已实现 | openai_compatible 适配器仅环境变量配置；无 Key 自动离线；内容守卫拦截金额/审批/状态；**真实模型未实测** |
| 微调实验入口 | 骨架未运行 | `scripts/gen_sft_samples.py` 固定种子合成样本；无 GPU/Key 未训练，不声称任何效果 |
| 真实 LLM 指标、真实微调、生产级前端、外部接入、生产部署 | 未实现或未实测 | 不属于 V1.1 主链路；本地演示工作台已实现，但不具备生产身份与部署能力 |

当前 D 盘基线（2026-09-07 Agent Lab 边界剧本变更后实测）：未配置隔离 PG → **399 passed，44 skipped，1 warning**；设置 `OPSPILOT_TEST_DATABASE_URL` 指向唯一命名的隔离库 → **443 passed，0 skipped，1 warning**。隔离脚本还在两套独立数据库各完成 `25 passed`，并将迁移升至 `0005`。前一模式的跳过项表示未启用 PG live 测试，不能取代后一模式的 PG 验证；唯一警告来自 Starlette/AnyIO 的第三方弃用提示。数据为固定种子合成数据。破坏性 PG 集成只允许 `opspilot_test_*`@localhost（fail-closed guard，见 `docs/POSTGRES.md`）。

已清理早期退款服务、Mule Bridge、SQLite 快照恢复和 PgBackedSession 整库镜像原型，避免双实现和额外协议面。

主要风险：真实模型行为未知，靠离线基线和能力矩阵隔离；PG 需要本地 Docker 与迁移，面试前按 `POSTGRES.md` 复跑；文档每次随代码同步；所有临时、下载、缓存和 checkpoint 固定落 D 盘 `.runtime/` 或 `.cache/`。

下一步不增加检索层级、多 Agent 默认编排、真实微调、向量数据库或外部系统，只做可复现验收、演示和缺陷修复。
