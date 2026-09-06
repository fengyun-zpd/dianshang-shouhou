# 模型评测与受控 LLM 运行时（阶段 5A）

> 归属：电商售后多智能体工单系统（OpsPilot）。版本：v1.1（V1.1 升级核验）。
> 目标：在不扩大模型权限的前提下，建立**可替换 / 可评测 / 可安全降级**的 OpenAI-compatible LLM 适配层。
>
> **V1.1 实测边界（如实）**：`offline` 规则基线已实测（意图准确率 1.0 / golden-v1）；
> **真实 LLM 未实测**——需用户提供安全 Key（`OPSPILOT_LLM_API_KEY`）与白名单允许的
> Base URL（`OPSPILOT_LLM_BASE_URL` ∈ `OPSPILOT_LLM_ALLOWED_BASE_URLS` 或默认白名单）
> 后才能实测；未配置时一律安全降级到离线规则，**零网络请求**，报告标注"未实测/N/A"。

## 1. 能力矩阵（模型只能做低风险语言任务）

| 能力 | 说明 | 风险 | 默认 |
| --- | --- | --- | --- |
| `intent_classification` | 售后意图识别 | 低 | 启用 |
| `structured_extraction` | 缺参字段抽取 | 低 | 启用 |
| `clarification_copy` | 用户澄清话术 | 低 | 启用 |
| `evidence_explanation` | 基于已验证证据的方案解释 | 低 | 启用 |
| `tool_calling` | 模型自主选择工具 | 高 | **禁用** |
| `high_risk_draft` | 高风险动作草稿 | 高 | **禁用** |

规则：即使能力矩阵放行，模型输出也只是**语言结果**；金额、资格、审批、状态与写操作永远由确定性领域服务裁决。`ModelGateway.run_task` 对高风险任务直接抛 `ModelContentPolicyError`（测试覆盖）。

## 2. 运行时结构与安全降级

`src/models/`：`config.py`（环境变量安全配置 + Base URL 白名单 + Key 校验）→ `base.py`（统一 `LLMClient` 协议、异常体系、内容安全守卫）→ `schemas.py`（Pydantic 结构化输出：`IntentExtraction` / `ClarificationDecision` / `EvidenceBoundExplanation` / `ModelInvocationMetadata`）→ `prompts.py`（版本化 Prompt，内嵌安全红线）→ `offline.py`（规则基线适配器）→ `openai_compatible.py`（HTTP 适配器）→ `router.py`（能力矩阵 + `ModelGateway` 降级链）。

触发安全降级（降级到离线规则或转人工信号）：
- API Key 缺失 / Base URL 不在白名单 → `ModelConfigError`（构造即失败，**零网络请求**，测试断言）；
- 超时 / 网络失败 → `ModelTimeoutError` / `ModelNetworkError`（有限重试后仍失败）；
- HTTP 429 → `ModelRateLimitedError`；5xx → `ModelHttpError`；
- JSON 非法 → `ModelParseError`；Schema 不匹配 → `ModelSchemaError`；
- 输出含金额 / 审批 / 状态 / 执行指令 → `ModelContentPolicyError`（`assert_output_safe` 先于解析守门）；
- Token 预算超限 → `ModelQuotaExceededError`（未调用即拒绝）。

发送前防护：PII 脱敏（复用 `src/platform/redact.py`，测试断言发送内容无完整手机号）；证据块先做提示注入检测，命中即**拒绝发送**（零网络）。

## 3. 影子模式

- 正常业务继续使用离线规则（不接模型）；
- 候选模型只对同一输入做预测，**零业务副作用**（不创建领域服务/不写审计/不驱动审批；专项测试断言领域状态不变）；
- 对照：离线预测 / 候选模型预测 / 黄金集期望。

运行入口（D 盘 `.venv`）：

```powershell
.venv\Scripts\python.exe evals\run_model_shadow_eval.py --mode offline
.venv\Scripts\python.exe evals\run_model_shadow_eval.py --mode candidate
```

`candidate` 必须显式配置安全环境变量（`OPSPILOT_LLM_API_KEY`、`OPSPILOT_LLM_BASE_URL` ∈ 白名单）；未配置时安全降级——不联网、报告明确写"未实测"。

报告：`evals/reports/shadow_eval_<mode>.md`。必填字段：模型名与版本、Prompt 版本、数据集版本、运行时间、Token、成本、耗时、错误率、降级数。

## 4. 真实指标与"未实测"边界

| 项 | 数值（2026-09-04，本机实测） |
| --- | --- |
| 离线模式（offline/rules-v1）意图准确率 | 1.0（黄金集 golden-v1，错误率 0） |
| 离线 P95 耗时 | <1 ms（规则，无网络） |
| 候选模型 | **未实测**（本环境未配置 OPSPILOT_LLM_API_KEY；安全降级、未联网） |
| Token / 成本 | 估算记账；真实模型调用后回填 |

**规则：没有真实运行并保存报告，不得声称真实模型已验证**；字段级准确率在提供结构化字段黄金集前标"未实测"。

## 5. 记账与成本记录

`ModelInvocationMetadata` 记录 provider/model/model_version/task/prompt_version/dataset_version/duration_ms/input_tokens/output_tokens/cost_estimate_usd/degraded/error。Token 为估算（字符/4）；单价经 `OPSPILOT_LLM_COST_PER_1K_IN` 配置（未配置按 0 记账并标注 N/A）。

## 6. 后续微调门禁（阶段 5 后续，本阶段不做）

- 无对照数据禁止 LoRA/QLoRA/DPO；
- 微调须先跑同一黄金集/影子集并保存对照报告；
- 微调模型**不得**获得额外业务写权限，能力矩阵与内容守卫对微调模型同样生效。

## 7. 修订记录

- v1.0（2026-09-04）—— 建立能力矩阵、运行时结构、降级策略、影子模式、指标与诚实边界。
- v1.1（V1.1 升级核验）—— 逐条核对阶段 2 验收：配置仅读环境变量、无 Key 默认离线、
  结构化 Schema 校验、内容守卫（金额/审批/状态/执行词）覆盖、经 ModelGateway 且配置
  Key 时走主模型而非离线的路径选择测试已存在（tests/unit/models/test_router.py 的
  `test_gateway_primary_success_not_degraded`）、影子报告字段齐全且候选未配 Key 标注
  "未实测"；本轮无代码改动（核实结论），仅明确 V1.1 实测边界与文档版本。
