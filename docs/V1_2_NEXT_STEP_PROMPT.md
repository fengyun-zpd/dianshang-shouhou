# OpsPilot V1.2 下一步完整执行提示词

你现在接手 `D:\workplace\PyCharmMiscProject\私域`，负责 **V1.2 既有缺陷修复与最终验收**。
直接完成实现、回归、证据落盘与文档同步，不停留在建议、计划或伪代码。按既有授权自主完成
可逆的本地修复；遇到真实外部阻塞，完成其他独立工作并记录阻塞，不伪造通过。

## 先确认事实与范围

1. 先读 `AGENTS.md`、`README.md`、`docs/V1_2_REVIEW_2026-09-13.md`、
   `docs/ARCHITECTURE.md`、`docs/POSTGRES.md`、`docs/TESTING_BASELINE.md`、
   `docs/STATUS_AND_RISKS.md`、`docs/DEFECTS_LOG.md`。
2. 检查当前 HEAD 与工作区。审查基线为 `d5ed51f`，接手时可能已变化；按当前代码重新定位，
   不假设行号不变。已有未提交 `炼化.md`，保留其原有结构和用户内容，只做本任务相关增补，
   禁止 reset/checkout 覆盖或批量删除。`docs/V1_EXECUTION_PLAN.md` 的旧提示词仅历史留档，
   不据此删除现有模型、Judge 或实验模块。
3. 本轮属于工作流、领域接口适配、评测和文档缺陷修复。先简述状态、权限、错误、幂等与审计
   设计再写代码。维持单 Agent 默认、PostgreSQL 业务事实源、checkpoint 流程状态、人审决定
   先落领域事实、unknown 只按原操作查询/对账。GET 接口不产生写副作用。
4. 不增加默认多 Agent、微调/DPO、pgvector、长期记忆、生产身份、真实支付/CRM/企业微信。
   不调用真实 LLM/Judge，不安装新平台，不扩大功能范围。发现确需新增业务副作用或改变事实源，
   先完成可独立的本地工作，再说明具体变更并依项目宪法请求确认。
5. 运行前执行 `. .\scripts\init_d_env.ps1`。Python 固定使用 `.venv\Scripts\python.exe`。
   缓存、日志、checkpoint、临时数据全部在 D 盘 `.runtime/` 或 `.cache/`，不向 C 盘安装或写入。

## 先建立失败证据

运行 `scripts/review_v12_boundaries.py`；具备隔离 PG 后加 `--pg`。脚本当前预期退出码为 1：
memory 六个检查失败，加入 PG 后共七个失败。把输出保留到本轮独立运行目录。
脚本 ERROR 代表复现未完成，不能算证明缺陷或修复通过。不能删除检查、改低标准或硬编码 PASS。

审查已实测：离线 490 passed / 49 skipped；隔离 PG 全量 538 passed / 1 failed，失败项为
`test_restart_recovers_thread_and_continues`，单独重跑 1 passed。没有代码修复的重跑成功不能
抹去原失败。真实 LLM、Judge、性能、生产接入和微调仍未实测。以下逐项补能验证业务结果的回归，
先证明能失败，再做最小修复；不要写仅复述实现细节的测试。

## 第一优先级：R1 租户隔离

问题在 `WorkflowRunner._cfg_for/_thread_exists/_resolve_tenant/get_state`：冒号拼接不是
无歧义的二元组编码。`(T1:dept, victim)` 与 `(T1, dept:victim)` 会共用 checkpoint。
memory 可直接读到他租户状态；PG 先尝试 start 留下本租户线程行后，即使 start 409，GET 仍泄露。

实施要求：

- 建立统一、带版本、无歧义的 checkpoint 键编码，使用结构化编码或等价安全方案；所有读写、
  start/resume/state、loop-stop、Supervisor 共享同一个实现。支持现有允许的冒号等标识。
- 查询到 checkpoint 后核对其租户、线程及请求绑定；数据库行存在不能替代 checkpoint 归属校验。
  先验证，再返回内容或推进图。错误租户仍是统一 404，绝不回显真实所属租户或内部状态。
- 处理旧 `tenant:thread` checkpoint：只能在元数据和既有线程事实匹配后读取/迁移；歧义或
  损坏时 fail-closed。不要删除原 checkpoint，也不要盲目旧键 fallback 重新引入碰撞。
- 未显式提供 tenant 的脚本便利入口，只能在绑定唯一时使用；多租户同名线程应要求显式 tenant，
  不能选择最近访问的租户。HTTP 一律使用认证身份。
- 回归覆盖普通 T1/T2、冒号碰撞、同租户同请求复用/异请求 409、伪造 checkpoint 元数据、
  旧键合法恢复与歧义拒绝、新实例读取、PG 冲突 start 后 GET。检查越权请求零新增业务写入。

## 第二优先级：R2 对外脱敏

`src/api/app.py::_agent_view` 原样返回 `state.user_request`；审批人可读到原始联系方式。

- 定义公共 Agent 视图与公共嵌套 state 字段，保留界面所需 thread、operation、ticket、金额、
  证据、错误码、步数与审计信息；不要直接序列化全部内部 checkpoint。
- 统一处理 start/clarify/decision/state 的用户文本、reply、interrupt、嵌套展示文本；复用
  现有脱敏能力。核查 422 校验错误的 `input` 回显、错误处理与日志，避免另一入口泄露原文。
- 脱敏不能改变业务对象标识、金额、领域错误码、审批版本或幂等判断。请求指纹应保留对原始
  请求差异的区分能力，不得对掩码后的相同文本计算指纹而合并不同请求。
- 用合成手机号、邮箱、身份证覆盖四接口、澄清、校验失败和日志；同时验证正常退款金额与
  幂等行为未变。报告只记录是否泄露，避免测试日志本身打印完整 PII。

## 第三优先级：R4 对账与执行后关单

`apply_decision` 在操作已 EXECUTED/FAILED 时直接结束；unknown 到 END 后，领域 reconcile
虽成功，Agent decision 仍 409、state 仍 unknown、工单仍 open。修复已有退款业务闭环，
不创造新的审批或退款执行通道。

- 先写明确状态转移表：PENDING_APPROVAL 等待；APPROVED 经领域授权执行；UNKNOWN 仅原键
  对账；EXECUTED/FAILED/REJECTED 根据真实领域状态进入允许的收尾；循环保护终态不能重启执行。
- 复用现有 `decision` 空 body 作为 APPROVER/SYSTEM 的显式恢复入口，使待对账/待收尾的
  既有线程可以重读事实并关单。客户和 AGENT 不可通过该入口执行恢复；body 仍禁止审批结果、
  金额或外部结果。对账结果仍只通过原领域接口由 SYSTEM 提交。
- UNKNOWN 未确认时不 execute、不关单、不换键。已 EXECUTED 时绝不再次 execute；只读取
  操作事实并调用现有领域关单命令。任何错误保留原错误码和人工处理信息，不能改写为成功。
- GET state 保持只读，可明确区分流程快照与当前领域状态；不能在页面伪改 outcome 来掩盖
  未关单。处理旧 checkpoint 已 END 且 next_action=reconcile_required 的恢复，禁止从 parse
  重跑并重新建单，也禁止从 HTTP 接受任意节点跳转参数。
- 覆盖 success/failed 对账、外部已执行后恢复、执行已提交但 checkpoint 未更新、关单失败
  后恢复、重复恢复、重启恢复与原键幂等。断言最终 ticket 状态/决议、operation 状态、退款
  累计、execute 与 close_ticket 次数、审批事实未伪造；不能只断言 HTTP 200 或回复文案。

## 第四优先级：R3 审计关联

`AfterSalesGateway.collect_audit_events` 的全局水位导致 T2 事件遗漏、同租户 A/B 串入审批事件。

- 不能仅把水位改成 tenant 字典：还要按当前线程绑定的 ticket/operation 过滤实际领域事件。
  从领域审计事实读取关联，不把 checkpoint 或进程内水位变成审计事实源。
- 确保稳定事件标识、无重复、可重建；重启后和旧线程并存时都不依赖进程创建顺序。若增加
  只读端口方法，MemoryAdapter/PgCommandAdapter 一起实现，避免上层按后端类型分支。
- 在本轮涉及的所有返回路径收集相关事件：等待、拒绝、已执行、unknown、对账、收尾失败。
  不伪造领域未产生的事件，不改变领域事实或生成假审计编号。
- 回归至少覆盖 T1→T2→T1、同租户两个线程交错审批、重复 state/decision、新实例恢复及 PG。
  断言他线程事件不出现、本线程预期事件完整、重读不重复。

## 第五优先级：R5 恢复测试稳定性与真实进程证据

- 阅读并保留本次 `.runtime/review-20260913/pytest.log`（若存在）。复查全量时 409 与单项通过
  的差异。根因尚不明确，不能先假定租约实现有错或只靠加长 sleep 处理。
- 记录脱敏后的租约 owner/generation/DB 当前时间/lease_until；确认初始化与测试之间无共享
  数据干扰。有限等待必须以数据库判定过期为依据；失败需明确超时，不能无限重试或跳过断言。
- 现有重启测试只是同一 Python 进程的两个实例。增加有不同 PID 的子进程 A/B（各自独立
  runner/checkpointer），同一隔离 PG 与同一 D 盘 checkpoint：A 经 HTTP 启动并等待审批，
  退出 A；B 经 HTTP 读 state、提交带版本的审批事实、decision 恢复并关单。
- 同时验证未过期租约拒绝接管、错误租户 404、仅一次退款、审计不串单、loop-stop 重启后
  保持终态。进程异常需有限超时并清理本次启动的子进程，Windows 不弹额外控制台窗口。
- 若调查发现同 owner 并发双推进等额外缺陷，先提供独立复现再修最小根因；不能把待核查风险
  写成已证实事故。没有多实例部署验证就维持“未验证”口径。

## 最后：R6 文档与演示对齐

- 同步 README、STATUS_AND_RISKS、TESTING_BASELINE、ARCHITECTURE、POSTGRES、
  INTERVIEW_OVERVIEW 和缺陷台账，记录 R1–R6 对应的当前修复/验证状态与回归位置。
  新增编号不要覆盖既有 D1–D12；本轮审查记录保留为历史证据，另追加修复结论。
- `炼化.md` 基于旧 HEAD：只对齐 HTTP 已接通、当前测试数字及功能状态的相关段落；保留学习
  结构和用户原文中的无关内容。如难以安全增补，另存 V1.2 勘误文档，不全文件替换。
- 模型/Judge 保持“实现完成、离线验证、真实模型未实测”，不用旧提示词删除现有模块。
  远程 CI 没有实际查询证据就不写“当前全绿”。
- 界面仅补本轮状态恢复、关单、脱敏和审计展示；走真实受保护 API，不硬编码成功。涉及前端
  时检查正常、拒绝、unknown→对账→恢复、跨租户、循环终态等路径；记录浏览器是否实际验证。

## 可复现验收命令与纪律

```powershell
. .\scripts\init_d_env.ps1
.venv\Scripts\python.exe scripts\review_v12_boundaries.py
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe scripts\demo_agent_http.py
.venv\Scripts\python.exe scripts\demo_interview.py
node --check src/api/ui/workspace.js
git diff --check
```

先运行修复相关测试，再做上述最终验证。黄金集、离线对照沿用项目现有命令，报告如需生成，
先确认支持的输出参数并写本轮 `.runtime/`，保留 canonical 旧证据，最终确需更新时再有意更新。

PG：先检查 `src/platform/pg_test_guard.py` 与当前隔离脚本。创建唯一命名的本地
`opspilot_test_*` 库并迁移到 head，建库或迁移失败立即停止该路径，禁止复用共享库。
测试唯一目标为显式 `OPSPILOT_TEST_DATABASE_URL`，不从通用 `DATABASE_URL` 偷偷回退。
守卫必须先于探测、连接、DROP/重建。不要把本文历史隔离库名写死为新一轮默认目标。

```powershell
# 在刚创建并迁移的隔离库已显式设置 OPSPILOT_TEST_DATABASE_URL 后：
.venv\Scripts\python.exe scripts\review_v12_boundaries.py --pg
.venv\Scripts\python.exe -m pytest tests -q
.\scripts\run_pg_tests_isolated.ps1
```

保存每次命令、退出码、完整测试摘要、HEAD、日期、数据集/Prompt/模型版本、运行模式与合成边界。
PG 不可用就列出未验证项，不用 memory 代替。全量仍失败时保留失败，继续定位；单项重跑成功
不能直接标全量通过。针对已观察到的不稳定项，使用有上限的重复验证并报告次数，不能循环到通过。

## 完成定义与最终交付

只有 R1–R4 正常与失败回归、PG 事实检查、R5 独立进程恢复和必要全量验证通过，才建议最终验收。
否则继续保持 RC，并逐项解释剩余阻塞。真实模型、微调、性能、生产接入永远不因本轮通过而升级口径。

交付代码与针对性测试、实际验收报告、缺陷闭环对照表、文档同步以及一条可复现的演示顺序。
最终回复简要说明修复内容、实测 passed/failed/skipped、仍未验证项与证据路径。每个“完成”必须
有实现和测试依据。不要声称本审查脚本退出 1 是通过，也不要称未执行的测试为已验证。
除非用户另行要求，不提交、推送、合并或部署；无需为本轮既定范围内的可逆修复反复询问。

## 本提示词执行记录（2026-09-13）

本提示词已在当前工作树完整执行，用户已明确授权提交并推送到目标 GitHub 仓库。R1–R5 均已修复并回归：

- R1：checkpoint v2 长度编码、旧键精确兼容和歧义拒绝；
- R2：HTTP 公共视图/错误响应白名单与 PII 脱敏；
- R3：按租户、线程和 ticket/operation 实体关联审计；
- R4：原 operation 的 unknown 对账、已执行恢复和失败收尾；
- R5：独立子进程重启验收，并以 PostgreSQL 时间判断租约过期。

执行结果：离线 `502 passed / 55 skipped`；隔离 PG `557 passed / 0 skipped`；边界脚本 R1–R4 全部 PASS；隔离双跑两套库各 `25 passed`。真实模型、微调、性能、多实例部署和生产外部系统仍保持未实测/未实现口径。
