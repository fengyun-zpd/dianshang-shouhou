# 测试基线（TESTING BASELINE）

> 归属：电商售后多智能体工单系统（`D:\workplace\PyCharmMiscProject\私域`）
> 版本：v0.1。本文档定义测试分层、验收命令、Bug 分类与回归纪律。

## 1. 分层策略与验收命令

| 层 | 覆盖内容 | 目录 | 当前状态 |
| --- | --- | --- | --- |
| unit | 纯函数 / 领域服务 / 权限 / 状态机 / 幂等 | `tests/unit/`、`tests/test_refund_service.py` | ✅ 45 项全绿（含退款最小闭环 14 项） |
| integration | 工具与数据库集成（TenantContext、真实仓储） | `tests/integration/` | 规划中（PostgreSQL 引入后） |
| e2e | 端到端流程 / SSE / 审批中断恢复 | `tests/e2e/` | 规划中（阶段 2+） |
| property | 性质测试（状态机不变量、金额守恒） | `tests/property/` | 规划中 |
| security | 越权 / 提示注入 / 租户隔离 / 未知状态 | `tests/security/` | 规划中（阶段 3+） |

验收命令（项目根执行）：

```powershell
python scripts/run_tests.py            # 分层 + 全量回归（推荐入口）
python scripts/run_tests.py --quick    # 快速冒烟
python -m pytest tests/ -v             # 全量明细
```

> 说明：脚本显式 `-p no:cacheprovider`，规避中文路径下 pytest cache 写失败（见 `docs/STATUS_AND_RISKS.md` R2）。`tests/conftest.py` 提供 `FIXED_SEED=42` 固定种子与合成数据辅助（`sample_money` / `make_id`），不依赖领域实体。

## 2. 回归纪律

1. **先补回归测试，再改实现**（RED → GREEN）。
2. 每次变更必须运行最小相关回归；涉及 Prompt / 模型 / 工具 Schema / 检索器 / 状态机的变更，必须跑同一全量回归并锁定版本。
3. 不硬编码成功响应掩盖工具失败；领域错误码不得被改写成成功。

## 3. Bug 分类

| 分类 | 含义 | 修复时回归重点 |
| --- | --- | --- |
| domain | 规则或状态机错误 | 对应状态机/金额/权限单测 |
| agent | 路由 / 提示 / 工具选择 | 意图/工具调用用例 |
| rag | 召回 / 引用 / 注入 | Recall/MRR、引用正确率、注入用例 |
| reliability | 超时 / 重试 / 未知状态 | operation_unknown 用例 |
| security | 越权 / 泄露 | 权限矩阵、租户隔离 |
| ui | 状态可见性 | 前端用例（阶段 2+） |
| ops | 部署 / 观测 | 启动与观测冒烟 |

每个 Bug 记录：最小复现、输入哈希、期望不变量、修复模块、回归测试。

## 4. 回归报告模板

```markdown
# 回归报告
- 日期：YYYY-MM-DD
- 代码版本（git）：<commit>
- Python / pytest：3.12.x / 9.x
- 模型版本：N/A（当前无 LLM 运行时）   # 阶段 2+ 填写
- Prompt 版本：N/A
- 数据集版本：N/A（固定种子合成数据 seed=42）
- 运行模式：本地内存仓储
- 结果：通过 X / 失败 Y / 跳过 Z（python scripts/run_tests.py）
- 安全不变量检查（必须全 0，任一非 0 即阻断问题）：
  - 越权成功数：0
  - 重复副作用数：0
  - 未知状态盲目重试数：0
  - 非法状态迁移数：0
- 变化说明：<本次变更与影响模块>
```

## 5. 修订记录

- v0.1（本会话）—— 建立分层策略、验收命令、Bug 分类与回归报告模板。
