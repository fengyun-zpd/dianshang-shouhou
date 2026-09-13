# LLM-as-Judge 评测报告

## 运行标识

- 裁判模型：`offline/rule-judge-v1`
- 裁判版本：`1.0`
- 裁判 Prompt 版本：`N/A（规则裁判）`
- Rubric 版本：`1.0`
- 运行模式：`offline`
- 实测状态：未实测（离线规则裁判）
- 真实裁判启用：False；未降级实测条数：0
- 网络请求数：0
- 样本数：11
- 降级数：0

## 聚合分数

- 各维度均分：accuracy=5；completeness=5；clarity=1.73；tone=3.18；citation_alignment=3.55；boundary_disclosure=4.64
- 各维度最低分：accuracy=5；completeness=5；clarity=1；tone=3；citation_alignment=3；boundary_disclosure=1
- 平均总分：23.09 / 30

## 确定性业务结论（Judge 不替代）

- 已提供确定性结论：0 条
- 业务断言失败（business_ok=False）：0 条
- Judge 分数不能替代业务断言（金额/权限/审批/状态/幂等/副作用由确定性检查负责）。
- 本报告不运行确定性业务验收（由 evals/replay.py 负责）；Judge 分数不改变确定性业务结论。

## 逐条结果

| case_id | rubric_version | judge_model | scores | total/max | short_reason | degraded | error | business_ok |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| g01-refund-happy | 1.0 | offline/rule-judge-v1 | accuracy=5 completeness=5 clarity=1 tone=3 citation_alignment=5 boundary_disclosure=5 | 24/30 | 离线规则：accuracy=5(命中1/1要点)；completeness=5(覆盖1/1要点)；clarity=1(无澄清问句)；tone=3(平淡（中性）)；citation_alignment=5(引用一致1/1)；boundary_disclosure=5(含边界说明) | False | - | - |
| g02-refund-rejected | 1.0 | offline/rule-judge-v1 | accuracy=5 completeness=5 clarity=1 tone=3 citation_alignment=3 boundary_disclosure=5 | 22/30 | 离线规则：accuracy=5(命中1/1要点)；completeness=5(覆盖1/1要点)；clarity=1(无澄清问句)；tone=3(平淡（中性）)；citation_alignment=3(未提供引用基线（中性）)；boundary_disclosure=5(含边界说明) | False | - | - |
| g03-clarify-missing-order | 1.0 | offline/rule-judge-v1 | accuracy=5 completeness=5 clarity=5 tone=5 citation_alignment=3 boundary_disclosure=1 | 24/30 | 离线规则：accuracy=5(命中1/1要点)；completeness=5(覆盖1/1要点)；clarity=5(含澄清问句)；tone=5(礼貌标记充分)；citation_alignment=3(未提供引用基线（中性）)；boundary_disclosure=1(未说明边界) | False | - | - |
| g04-escalate-order-not-found | 1.0 | offline/rule-judge-v1 | accuracy=5 completeness=5 clarity=1 tone=3 citation_alignment=3 boundary_disclosure=5 | 22/30 | 离线规则：accuracy=5(命中1/1要点)；completeness=5(覆盖1/1要点)；clarity=1(无澄清问句)；tone=3(平淡（中性）)；citation_alignment=3(未提供引用基线（中性）)；boundary_disclosure=5(含边界说明) | False | - | - |
| g05-escalate-no-policy | 1.0 | offline/rule-judge-v1 | accuracy=5 completeness=5 clarity=1 tone=3 citation_alignment=3 boundary_disclosure=5 | 22/30 | 离线规则：accuracy=5(命中1/1要点)；completeness=5(覆盖1/1要点)；clarity=1(无澄清问句)；tone=3(平淡（中性）)；citation_alignment=3(未提供引用基线（中性）)；boundary_disclosure=5(含边界说明) | False | - | - |
| g06-escalate-conflict-policy | 1.0 | offline/rule-judge-v1 | accuracy=5 completeness=5 clarity=1 tone=3 citation_alignment=3 boundary_disclosure=5 | 22/30 | 离线规则：accuracy=5(命中1/1要点)；completeness=5(覆盖1/1要点)；clarity=1(无澄清问句)；tone=3(平淡（中性）)；citation_alignment=3(未提供引用基线（中性）)；boundary_disclosure=5(含边界说明) | False | - | - |
| g07-escalate-unsupported-intent | 1.0 | offline/rule-judge-v1 | accuracy=5 completeness=5 clarity=3 tone=3 citation_alignment=3 boundary_disclosure=5 | 24/30 | 离线规则：accuracy=5(命中1/1要点)；completeness=5(覆盖1/1要点)；clarity=3(澄清意图不完整)；tone=3(礼貌标记一般)；citation_alignment=3(未提供引用基线（中性）)；boundary_disclosure=5(含边界说明) | False | - | - |
| g08-escalate-unknown-intent | 1.0 | offline/rule-judge-v1 | accuracy=5 completeness=5 clarity=3 tone=3 citation_alignment=3 boundary_disclosure=5 | 24/30 | 离线规则：accuracy=5(命中1/1要点)；completeness=5(覆盖1/1要点)；clarity=3(澄清意图不完整)；tone=3(礼貌标记一般)；citation_alignment=3(未提供引用基线（中性）)；boundary_disclosure=5(含边界说明) | False | - | - |
| g09-operation-unknown-recovery | 1.0 | offline/rule-judge-v1 | accuracy=5 completeness=5 clarity=1 tone=3 citation_alignment=3 boundary_disclosure=5 | 22/30 | 离线规则：accuracy=5(命中1/1要点)；completeness=5(覆盖1/1要点)；clarity=1(无澄清问句)；tone=3(平淡（中性）)；citation_alignment=3(未提供引用基线（中性）)；boundary_disclosure=5(含边界说明) | False | - | - |
| g10-repeat-request-idempotent | 1.0 | offline/rule-judge-v1 | accuracy=5 completeness=5 clarity=1 tone=3 citation_alignment=5 boundary_disclosure=5 | 24/30 | 离线规则：accuracy=5(命中1/1要点)；completeness=5(覆盖1/1要点)；clarity=1(无澄清问句)；tone=3(平淡（中性）)；citation_alignment=5(引用一致1/1)；boundary_disclosure=5(含边界说明) | False | - | - |
| g11-forged-resume-safe | 1.0 | offline/rule-judge-v1 | accuracy=5 completeness=5 clarity=1 tone=3 citation_alignment=5 boundary_disclosure=5 | 24/30 | 离线规则：accuracy=5(命中1/1要点)；completeness=5(覆盖1/1要点)；clarity=1(无澄清问句)；tone=3(平淡（中性）)；citation_alignment=5(引用一致1/1)；boundary_disclosure=5(含边界说明) | False | - | - |

## 说明

- 离线规则裁判（不联网、零成本、确定性打分；不评估金额/权限/审批/状态/幂等/副作用）
- 真实 Judge 未实测；当前结果为离线规则裁判；未产生网络请求。
- 不得将本报告中的任何分数表述为真实模型的准确率、成本或延迟。

## 声明（不可省略）

- Judge 不能替代业务断言（金额/权限/审批/状态/幂等/副作用由确定性检查负责）；
- Judge 分数未经人工校准时只能作为参考；
- 没有真实模型时属于离线/未实测。

> 诚实边界：本报告不含 API Key、完整请求体或客户 PII；真实 Judge 必须实际运行并保存报告后才能声称已实测，未运行一律标注未实测。Judge 分数不改变确定性业务结论。