# 影子评测报告（售后意图与澄清识别）

## 运行标识

- 候选模型：`offline/rules-v1`
- 模型版本：`1.0`
- Prompt 版本：`1.0`
- 数据集版本：`golden-v1`
- 运行时间：2026-09-13 17:47:25（耗时 0.0 s）
- 运行模式：`offline`（provider `offline-rule`）
- 网络请求数：0（未联网）

## 指标

- 总样本数：11
- 意图准确率：1.0
- 澄清识别结果：1 条需澄清（g03-clarify-missing-order）
- 结构化输出错误数：0
- 内容安全拦截数：0
- 降级次数：0
- 其他错误数：0（错误率 0.0）
- P50 延迟：0.0 ms｜P95 延迟：0.0 ms
- 业务副作用：0（影子模式仅文本预测，未调用任何领域写路径）

## Token 与成本

- 输入 token：0
- 输出 token：0
- 总 token：0
- 输入成本：N/A
- 输出成本：N/A
- 总成本：N/A
- 单条平均成本：N/A
- 价格配置来源：N/A（离线规则不调用模型，无 token 计费）

## 说明

- 离线规则基线（不联网、零成本：tokens=0，cost=N/A，provider=offline-rule）
- 成本说明：价格为 None 表示**未配置**（表中显示 N/A），不代表 0 成本；离线规则模式不调用模型，token 记为 0。

## 逐条明细

| case | provider | intent(预测/期望) | 命中 | degraded | error | in_tok | out_tok | cost | 耗时ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| g01-refund-happy | offline-rule | refund/refund | ✅ | False | - | 0 | 0 | N/A | 0.0 |
| g02-refund-rejected | offline-rule | refund/refund | ✅ | False | - | 0 | 0 | N/A | 0.0 |
| g03-clarify-missing-order | offline-rule | refund/- | ✅ | False | - | 0 | 0 | N/A | 0.0 |
| g04-escalate-order-not-found | offline-rule | refund/refund | ✅ | False | - | 0 | 0 | N/A | 0.0 |
| g05-escalate-no-policy | offline-rule | refund/refund | ✅ | False | - | 0 | 0 | N/A | 0.0 |
| g06-escalate-conflict-policy | offline-rule | refund/refund | ✅ | False | - | 0 | 0 | N/A | 0.0 |
| g07-escalate-unsupported-intent | offline-rule | exchange/exchange | ✅ | False | - | 0 | 0 | N/A | 0.0 |
| g08-escalate-unknown-intent | offline-rule | unknown/unknown | ✅ | False | - | 0 | 0 | N/A | 0.0 |
| g09-operation-unknown-recovery | offline-rule | refund/refund | ✅ | False | - | 0 | 0 | N/A | 0.0 |
| g10-repeat-request-idempotent | offline-rule | refund/refund | ✅ | False | - | 0 | 0 | N/A | 0.0 |
| g11-forged-resume-safe | offline-rule | refund/refund | ✅ | False | - | 0 | 0 | N/A | 0.0 |

> 诚实边界：全部为固定随机种子合成数据；报告不含 API Key、完整请求体或客户 PII。真实模型必须实际运行后回填，未运行一律标注未实测。