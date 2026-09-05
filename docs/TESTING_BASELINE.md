# 测试基线（TESTING BASELINE）

> 归属：电商售后多智能体工单系统（`D:\workplace\PyCharmMiscProject\私域`）
> 版本：v0.8。本文档定义测试分层、验收命令、Bug 分类与回归纪律。
> 全量基线（2026-09-05 本地实测）：`.venv\Scripts\python.exe -m pytest tests/ -q` → **417 passed, 0 xfailed**（PostgreSQL 容器运行时集成 33/33 实测；无 PostgreSQL 时集成自动跳过；缺陷台账 D1–D12 已全部修复）。

## 1. 分层策略与验收命令

| 层 | 覆盖内容 | 目录 | 当前状态（收集数，全量 417） |
| --- | --- | --- | --- |
| unit | 纯函数/领域服务/权限/状态机/幂等/API安全与 health/RAG/评测/桥接/Repository 契约/PG-backed codec/持久 checkpoint/PG-first 命令服务/rules/ports与 adapters/runtime profile | `tests/unit/` | ✅ 331 项全绿 |
| integration | PostgreSQL 实首（行锁/幂等三元组/0004数据面/PG-first 命令单事务与并发 CAS/PG profile HTTP e2e/0005约束/D9 租约 live | `tests/integration/` | ✅ 33 项 |
| e2e | 端到端流程/审批中断恢复 | `tests/e2e/` | ✅ 2 项 |
| property | 性质测试 | `tests/property/` | ✅ 12 项 |
| security | 越权/注入/租户隔离/未知态/PII | `tests/security/` | ✅ 4 项 |
| regression | 缺陷台账 D1–D12 | `tests/regression/` | ✅ 11 项 |
| phase4 | 阶段四契约（审批事实/幂等三元组/政策版本/D9 租约） | `tests/phase4/` | ✅ 10 项 |
| 根层 | 退款最小闭环 | `tests/test_refund_service.py` | ✅ 14 项 |

> 汇总：331 + 33 + 2 + 12 + 4 + 11 + 10 + 14 = **417 passed, 0 xfailed**（2026-09-05 实测）。


验收命令（项目根执行）：

```powershell
python scripts/run_tests.py            # 分层 + 全量回归（推荐入口）
python scripts/run_tests.py --quick    # 快速冒烟
python -m pytest tests/ -v             # 全量明细
```

> 说明：脚本显式 `-p no:cacheprovider`，规避中文路径下 pytest cache 写失败（见 `docs/STATUS_AND_RISKS.md` R2）。`tests/conftest.py` 提供 `FIXED_SEED=42` 固定种子与合成数据辅助（`sample_money` / `make_id`），不依赖领域实体。PostgreSQL 集成测试读取 `DATABASE_URL`（默认 `postgresql+psycopg2://opspilot:opspilot@127.0.0.1:5433/opspilot`），本机启动方式见 `docs/POSTGRES.md`。

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
| ui | 状态可见性 | 前端用例（未实现，无浏览器测试不声称完成） |
| ops | 部署 / 观测 | 启动与观测冒烟 |

每个 Bug 记录：最小复现、输入哈希、期望不变量、修复模块、回归测试。

## 4. 回归报告模板

```markdown
# 回归报告
- 日期：YYYY-MM-DD
- 代码版本（git）：<commit>
- Python / pytest：3.12.x / 9.x
- 模型版本：N/A（当前无真实 LLM 运行时；离线规则基线）   # 真实模型接入后填写
- Prompt 版本：N/A
- 数据集版本：N/A（固定种子合成数据 seed=42）
- 运行模式：本地内存领域服务 + MemorySaver checkpoint（PostgreSQL 集成另行实测）
- 结果：通过 X / 失败 Y / 跳过 Z（python scripts/run_tests.py）
- PostgreSQL：容器运行时集成 18/18 实测；无 PG 时自动跳过（数据库集成未实测）
- 安全不变量检查（必须全 0，任一非 0 即阻断问题）：
  - 越权成功数：0
  - 重复副作用数：0
  - 未知状态盲目重试数：0
  - 非法状态迁移数：0
- 变化说明：<本次变更与影响模块>
```

## 5. 修订记录

- v0.1（本会话）—— 建立分层策略、验收命令、Bug 分类与回归报告模板。
- v0.2（2026-09-04）—— 分层状态与基线同步实现：integration/e2e/property/security 由"规划中"更新为已实现并全绿（收集数 9/2/12/4，另有根层退款闭环 14 项）；unit 269 项；全量 **310 passed**（PG 容器运行时集成 9/9）；模板补充 PostgreSQL 实测/跳过说明与运行模式。
- v0.3（2026-09-04）—— Repository 增加 `unit_of_work` 命令级原子写作用域（K4 扩展）：契约测试 +4（unit 273 项）、PG 集成 +4（integration 13 项）；全量 **318 passed**（PG 容器运行时集成 13/13）。
- v0.4（2026-09-04）—— PG-backed 领域会话（`src/persistence/pg_backed.py`）：codec/roundtrip 单测 +7（unit 281 项，无 PG 依赖）；PG 集成 +4（integration 17 项：SQL 断言事实在 PG/roundtrip/save 失败整单位回滚/重启无重复副作用）；API 认证身份 `TokenResolver` 端口 +1；全量 **330 passed**（PG 集成 17/17）。
- v0.5（2026-09-04）—— 可持久化 LangGraph checkpoint（阶段三）：`src/agents/checkpoint.py`（SqliteSaver 落 SQLite 文件）+ 单测 +3（unit 284：跨实例/同文件重启 resume 不重放、注入伪造业务视图不覆盖领域事实、checkpoint 真实落盘）+ PG 集成 +1（integration 18：挂起审批→业务事实落 PG→重启后领域从 PG 重建+持久 checkpoint resume→审批/执行正确无重复副作用，SQL 断言 executed）；全量 **334 passed**（PG 集成 18/18）。
- v0.6（2026-09-04）—— 升级目标第一阶段：缺陷台账回归层 `tests/regression`（11 项 = 4 passed + 7 xfailed）；`docs/DEFECTS_LOG.md` 记录 D1–D12 证据/严重级/归属阶段；全量 **338 passed, 7 xfailed**（xfail=已知缺陷，修复后转 PASS，不伪造通过）。
