# 影子评测报告（阶段 5A）

- 模式：`candidate`
- 模型：`offline/rules-v1`（版本 `1.0`，provider `offline-rule`）
- Prompt 版本：`1.0`｜数据集版本：`golden-v1`
- 运行耗时：0.0 s

## 指标

- 用例数：11
- 意图准确率：1.0
- 识别需澄清数：1
- 错误数：0（错误率 0.0）
- 降级数：0
- 耗时：P50 0.0 ms / P95 0.0 ms
- Token：48｜成本：$0.0
- 业务副作用：0（影子模式仅文本预测，未调用任何领域写路径）

## 说明

- candidate 主模型未启用（安全失败）：未配置 OPSPILOT_LLM_API_KEY：模型候选模式已安全禁用（不会联网）。可先以 --mode offline 运行基线。。未联网；以下为主模型“未实测”的离线对照。
- 候选模型未实测（未配置安全 Key 或 Base URL 白名单校验失败，未联网）；以上为离线规则对照，不代表候选模型指标。

## 逐条明细

| case | provider | intent(预测/期望) | 命中 | degraded | error | tokens | 耗时ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| g01-refund-happy | offline-rule | refund/refund | ✅ | False | - | 5 | 0.0 |
| g02-refund-rejected | offline-rule | refund/refund | ✅ | False | - | 5 | 0.0 |
| g03-clarify-missing-order | offline-rule | refund/- | ✅ | False | - | 2 | 0.0 |
| g04-escalate-order-not-found | offline-rule | refund/refund | ✅ | False | - | 6 | 0.0 |
| g05-escalate-no-policy | offline-rule | refund/refund | ✅ | False | - | 5 | 0.0 |
| g06-escalate-conflict-policy | offline-rule | refund/refund | ✅ | False | - | 5 | 0.0 |
| g07-escalate-unsupported-intent | offline-rule | exchange/exchange | ✅ | False | - | 4 | 0.0 |
| g08-escalate-unknown-intent | offline-rule | unknown/unknown | ✅ | False | - | 1 | 0.0 |
| g09-operation-unknown-recovery | offline-rule | refund/refund | ✅ | False | - | 5 | 0.0 |
| g10-repeat-request-idempotent | offline-rule | refund/refund | ✅ | False | - | 5 | 0.0 |
| g11-forged-resume-safe | offline-rule | refund/refund | ✅ | False | - | 5 | 0.0 |

> 诚实边界：全部为固定随机种子合成数据；Token 为估算值；真实模型必须实际运行后回填。