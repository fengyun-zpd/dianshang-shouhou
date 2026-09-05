"""OpsPilot 源码包。

企业售后工单处置 Agent 与可靠性评测平台。已实现（代码+测试证据）：
确定性领域服务（订单核验/资格/退款上限/状态机/幂等/并发/审计）、LangGraph 单 Agent
工作流（审批 interrupt/resume）、受控工具契约与 TenantContext、RAG 政策证据（引用/
注入防护）、平台可靠性与黄金集评测、受控 LLM 适配（离线规则基线；真实模型未实测）、
Supervisor 只读子 Agent 实验、Mule Agent Bridge 本地契约、SQLite 恢复原型与
PostgreSQL Repository/schema/Alembic（数据库集成视本机 PG 是否运行而实测或跳过）。
规划中（未实现，不写成已实现）：审批工作台前端、真实模型接入、真实 MuleSoft/MCP
网络连接、微调（LoRA/QLoRA/DPO）、领域状态机整体 SQL 化。
"""
