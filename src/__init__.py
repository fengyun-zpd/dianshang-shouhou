"""OpsPilot 源码包。

企业售后工单处置 Agent 与可靠性评测平台。V1 已实现（代码+测试证据）：
确定性领域服务（订单核验/资格/退款上限/状态机/幂等/并发/审计，`after_sales/` 主实现）、
LangGraph 单 Agent 工作流（澄清/证据/审批 interrupt/resume/unknown 对账）、
受控工具契约与 TenantContext、最小政策 RAG（同租户启用版本 citation、注入防护；
不裁决金额/资格/状态）、PostgreSQL 业务事实源与 PG profile 命令路径
（PgCommandService/PgCommandAdapter/DB 租约；数据库集成视本机 PG 是否运行而实测或跳过）、
黄金集/回放/安全不变量评测、受控 LLM 适配（离线规则基线；真实模型未实测）、
Supervisor A/B 实验结论（与单 Agent 无量化收益 → 默认单 Agent）。

未实现/未实测（不写成已实现）：审批工作台前端、真实模型接入、真实 Mule/MCP
网络连接、微调（LoRA/QLoRA/SFT/DPO）、pgvector、长期记忆、生产部署。
"""
