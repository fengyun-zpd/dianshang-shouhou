# 电商售后模型评测与受控 LLM 运行时

> 归属：个人开发的电商售后数字化工具 OpsPilot。文档更新：2026-09-16；已记录实测：2026-09-13。
> 目标：在不扩大模型权限的前提下，建立**可替换 / 可评测 / 可安全降级 / 可记账**的
> OpenAI-compatible LLM 适配层。
>
> **实测边界（如实）**：`offline` 规则基线已实测（意图准确率 1.0 / golden-v1）；
> **真实 LLM 实现完成、离线验证、真实模型未实测**——需用户提供安全 Key
> （`OPSPILOT_LLM_API_KEY`）与白名单允许的
> Base URL（`OPSPILOT_LLM_BASE_URL` ∈ 默认白名单或 `OPSPILOT_LLM_ALLOWED_BASE_URLS`）
> **且**显式配置模型名（`OPSPILOT_LLM_MODEL`）后才允许联网；任一条件不满足即安全降级到
> 离线规则，**零网络请求**，报告标注"未实测/N/A"。

## 1. 能力矩阵（模型只能做低风险语言任务）

| 能力 | 说明 | 风险 | 默认 |
| --- | --- | --- | --- |
| `intent_classification` | 售后意图识别 | 低 | 启用 |
| `structured_extraction` | 缺参字段抽取 | 低 | 启用 |
| `clarification_copy` | 用户澄清话术 | 低 | 启用 |
| `evidence_explanation` | 基于已验证证据的方案解释 | 低 | 启用 |
| `tool_calling` | 模型自主选择工具 | 高 | **禁用** |
| `high_risk_draft` | 高风险动作草稿 | 高 | **禁用** |

模型只负责：意图抽取、缺参识别、澄清问题、解释文本。模型**不能**决定：金额、资格、
审批结果、状态迁移、幂等键、工具权限。即使能力矩阵放行，模型输出也只是语言结果；
`ModelGateway.run_task` 对高风险任务直接抛 `ModelContentPolicyError`（测试覆盖）。

## 2. 运行时结构与安全降级

`src/models/`：`config.py`（环境变量安全配置 + Base URL 白名单 + Key 校验 + 价格配置）→
`base.py`（统一 `LLMClient` 协议、异常体系、内容安全守卫）→ `schemas.py`（Pydantic 结构化输出
+ `ModelInvocationMetadata`）→ `prompts.py`（版本化 Prompt，内嵌安全红线）→ `offline.py`（规则基线
适配器）→ `openai_compatible.py`（HTTP 适配器）→ `router.py`（能力矩阵 + `ModelGateway` 降级链）。

触发安全降级（降级到离线规则或转人工信号）：

- API Key 缺失 / Base URL 不在白名单 → `ModelConfigError`（构造即失败，**零网络请求**，测试断言）；
- 超时 / 网络失败 → `ModelTimeoutError` / `ModelNetworkError`（有限重试后仍失败）；
- HTTP 429 → `ModelRateLimitedError`；5xx → `ModelHttpError`；
- JSON 非法 → `ModelParseError`；Schema 不匹配 → `ModelSchemaError`；
- 输出含金额 / 审批 / 状态 / 执行指令 → `ModelContentPolicyError`（`assert_output_safe` 先于解析守门）；
- Token 预算超限 → `ModelQuotaExceededError`（未调用即拒绝）。

发送前防护：**证据块先做提示注入检测（命中即拒绝发送、零网络），通过检测后逐块 `redact_pii`
脱敏，脱敏结果才参与 token 预算计数与请求拼接**；用户输入同样先脱敏。测试直接捕获 HTTP 请求体，
断言完整手机号 / 邮箱 / 身份证号既不出现在模型请求中，也不出现在日志中。

## 3. 候选模式（candidate）联网门禁

`evals/run_model_shadow_eval.py --mode candidate` 只有在以下条件**全部**满足时才联网：

1. `OPSPILOT_LLM_API_KEY` 存在；
2. `OPSPILOT_LLM_BASE_URL` 在白名单内（默认白名单：DeepSeek / OpenAI / Moonshot / DashScope，
   以及本机回环 `http://127.0.0.1`、`http://localhost` 供 mock 使用）；
3. 模型名显式配置（`OPSPILOT_LLM_MODEL`）——不显式配置就不联网，避免"默认模型"被误当成实测对象；
4. 价格配置明确，**或**报告明确标记成本为 `N/A`（价格缺失不阻断联网，但不得伪造成本）。

默认白名单（`DEFAULT_ALLOWED_BASE_URLS`）：`https://api.openai.com`、`https://api.moonshot.cn`、
`https://api.deepseek.com`、`https://dashscope.aliyuncs.com`、`http://127.0.0.1`、`http://localhost`。
校验按 scheme/hostname/端口/路径边界进行，拒绝 userinfo（`api.openai.com@evil.com`）与伪后缀域名。

代码中不写入真实 Key；不自动创建 `.env` 或写入凭证。没有 Key 时一律安全降级离线，
**不发送任何网络请求**（测试用 httpx 替身断言）。

## 4. 影子模式（零业务副作用）

- 正常业务继续使用离线规则（不接模型）；
- 候选模型只对同一输入做预测，**零业务副作用**：不创建领域服务/适配器、不建数据库连接、
  不写审计、不驱动审批（测试把 `AfterSalesService` / `MemoryAdapter` / `PgCommandAdapter` /
  `PostgresAfterSalesRepository` 的 `__init__` 替换为会抛错的替身后仍能跑完）；
- 对照：离线预测 / 候选模型预测 / 黄金集期望。

```powershell
.venv\Scripts\python.exe evals\run_model_shadow_eval.py --mode offline
.venv\Scripts\python.exe evals\run_model_shadow_eval.py --mode candidate
```

报告：`evals/reports/shadow_eval_<mode>.md`（**未实测的候选模式报告写入 `.runtime/reports/`**，
不入库，避免把离线降级写成真实候选模型成绩）。必填字段：候选模型、模型版本、Prompt 版本、
数据集版本、运行时间、运行模式、总样本数、意图准确率、澄清识别结果、结构化输出错误数、
内容安全拦截数、降级次数、输入 token、输出 token、总 token、输入成本、输出成本、总成本、
单条平均成本、价格配置来源、P50/P95 延迟、业务副作用：0。

## 5. Token 与成本记账

`ModelInvocationMetadata` 记录 provider / model_name / model_version / task / prompt_version /
dataset_version / duration_ms / input_tokens / output_tokens / `token_source` / `input_cost` /
`output_cost` / `cost_estimate_usd`（总成本）/ currency / pricing_source / degraded / error。

价格环境变量：

| 变量 | 含义 | 备注 |
| --- | --- | --- |
| `OPSPILOT_LLM_PRICE_PER_1K_INPUT` | 输入单价（每 1K token） | **新变量优先** |
| `OPSPILOT_LLM_PRICE_PER_1K_OUTPUT` | 输出单价（每 1K token） | 新变量 |
| `OPSPILOT_LLM_COST_PER_1K_IN` | 旧变量：仅输入单价 | 兼容保留；新变量存在时不生效 |
| `OPSPILOT_LLM_PRICE_CURRENCY` | 币种标记（默认 USD） | 仅用于展示，不做汇率换算 |

计算：`input_cost = input_tokens/1000 * input_price`；`output_cost = output_tokens/1000 * output_price`；
`total_cost = input_cost + output_cost`（**仅当输入与输出单价都已配置**）。

未配置/非法价格的行为：

- 未配置 → 对应成本项为 `None`，报告显示 `N/A`；
- 非法字符串（`abc` / 负数 / `nan` / `inf`）→ 安全回退为未配置，并把变量名记入
  `LLMSettings.price_errors`；
- 离线规则模式 → `tokens=0`、`cost=N/A`、`provider=offline-rule`、`token_source=not_applicable`：
  **不把离线规则的估算值冒充真实 LLM 成本**。

API Key、完整请求体与客户 PII 不写入日志或报告（测试断言 Key 不出现在报告与 stdout）。

## 6. LLM-as-Judge 评测骨架（可选，不替代确定性验收）

`evals/judge_rubric.json`（版本化 rubric）+ `evals/llm_judge.py`（裁判实现）+
`evals/run_llm_judge.py`（CLI）+ `evals/reports/judge_*.md`（报告）。

Judge 只评估**模糊质量**：解释准确性、解释完整性、澄清问题是否清楚、转人工措辞是否合适、
引用是否与输出一致、是否清楚说明 Agent 边界、语气是否专业。rubric 的 `out_of_scope` 显式列出
Judge **不得**评估或替代的维度：金额正确性、权限正确性、审批正确性、状态迁移、幂等、是否产生副作用。

- 输入先经 `redact_pii` 脱敏；只保留结构化理由摘要（`short_reason`，长度受限）与分数，
  不落模型内部完整推理（CoT）；输出 Schema 由 rubric 维度动态生成，分数越界即 Schema 错误；
- 默认离线规则裁判（确定性、不联网）；真实裁判 `LLMJudge` 需同时满足「安全 Key + 白名单
  Base URL + 显式模型名」才启用，且走既有 `OpenAICompatibleClient`（同一内容守卫、同一 PII
  脱敏、同一 Key 门禁），`http_post` 可注入 → **真实调用路径已实现并用 mock 单测**；
- 任何超时 / Schema 越界 / 内容守卫拦截都会降级为离线规则裁判并标记 `degraded=True` /
  `error=<异常类型名>`；**只要没有一条未降级的真实调用结果，报告仍按"未实测"输出**；
- Judge 结果与黄金集确定性结果**分开报告**；每条含 `case_id` / `rubric_version` / `judge_model` /
  `scores` / `short_reason` / `degraded` / `error`（另含 `total_score` / `max_score` / `business_ok`）；
- 报告显式声明：Judge 不能替代业务断言；分数未经人工校准时只能作为参考；没有真实模型时属于
  离线/未实测。**业务错误不会被 Judge 高分掩盖**（`deterministic_check` / `business_assertion_failed`
  独立标记，测试覆盖）。

```powershell
.venv\Scripts\python.exe evals\run_llm_judge.py --mode offline
.venv\Scripts\python.exe evals\run_llm_judge.py --mode judge
```

## 7. 真实指标与"未实测"边界

| 项 | 数值（2026-09-13，本机实测） |
| --- | --- |
| 离线模式（offline/rules-v1）意图准确率 | 1.0（黄金集 golden-v1，错误率 0，结构化错误 0，安全拦截 0） |
| 离线模式 token / 成本 | `tokens=0/0/0`，`cost=N/A`（离线规则不调用模型） |
| 候选模型 | **未实测**（本环境未配置 `OPSPILOT_LLM_API_KEY`；安全降级、零网络请求） |
| 候选模型真实 token / 成本 / 延迟 | **未实测**；配置价格后由报告回填 |
| 真实 Judge 分数 | **未实测**（仅离线规则裁判） |

**规则：没有真实运行并保存报告，不得声称真实模型已验证**；字段级准确率在提供结构化字段
黄金集前标"未实测"。不得虚构"DeepSeek-chat 已验证"、真实准确率、真实成本或真实延迟。

## 8. 后续微调条件（当前未运行）

- 无对照数据禁止 LoRA/QLoRA/DPO；
- 微调须先跑同一黄金集/影子集并保存对照报告；
- 微调模型**不得**获得额外业务写权限，能力矩阵与内容守卫对微调模型同样生效。

## 9. 修订记录

- （2026-09-04）—— 建立能力矩阵、运行时结构、降级策略、影子模式、指标与诚实边界。
- （2026-09-07）—— 核对模型适配验收；明确实测边界与文档依据。
- （2026-09-13）—— 新增输入/输出双向成本记账（新价格变量 + 旧变量兼容 + 未配置 N/A +
  非法值安全回退）、候选模式"模型名显式配置"门禁、影子报告完整成本列、影子零副作用与零网络
  断言、LLM-as-Judge 评测骨架（不替代确定性验收）。真实模型与真实 Judge 仍未实测。
