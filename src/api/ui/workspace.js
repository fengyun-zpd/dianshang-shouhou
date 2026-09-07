(() => {
  const state = { ticketId: null, operation: null, audit: [], labMode: "single_agent" };
  const $ = (selector) => document.querySelector(selector);
  const api = async (path, options = {}, token = "demo-agent") => {
    const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", "X-Api-Key": token, ...(options.headers || {}) } });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error(body.message || body.detail || `请求失败 (${response.status})`); error.status = response.status; error.code = body.code || body.detail?.code; throw error; }
    return body;
  };
  const toast = (message, error = false) => { const node = $("#toast"); node.textContent = message; node.className = `toast show${error ? " error" : ""}`; window.clearTimeout(toast.timer); toast.timer = window.setTimeout(() => node.className = "toast", 3600); };
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  const switchView = (viewId) => {
    document.querySelectorAll(".workspace-view").forEach((view) => { view.hidden = view.id !== viewId; view.classList.toggle("active", view.id === viewId); });
    document.querySelectorAll(".workspace-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.viewTarget === viewId));
    window.scrollTo({ top: 0, behavior: "smooth" });
  };
  const setStep = (index, status) => { document.querySelectorAll(".step").forEach((node, i) => { node.classList.toggle("done", i < index); node.classList.toggle("current", i === index); const label = node.querySelector(".step-state"); label.textContent = i < index ? "已完成" : i === index ? status : "未开始"; }); };
  const setButtons = ({ draft = false, approve = false, execute = false } = {}) => { $("#draft-action").disabled = !draft; $("#approve-action").disabled = !approve; $("#execute-action").disabled = !execute; };
  const formatStatus = (status) => ({ draft: "草稿已生成", pending_approval: "等待审批", approved: "已批准", executed: "退款已执行", operation_unknown: "结果待对账" }[status] || status || "待发起");
  const setOperation = (operation) => { state.operation = operation; $("#amount-value").textContent = operation.amount ? `¥${operation.amount}` : "--"; $("#amount-note").textContent = operation.amount ? "由适用政策与领域规则裁决" : "等待规则与政策校验"; $("#case-status").textContent = formatStatus(operation.status); $("#status-note").textContent = `操作版本 ${operation.version} · 可追溯`; };
  const renderAudit = () => { const list = $("#audit-list"); if (!state.audit.length) return; list.classList.remove("empty-state"); list.innerHTML = state.audit.slice(-6).reverse().map((item) => `<div class="audit-item"><span class="audit-dot"></span><div><strong>${item.action}</strong><p>${item.entity_type} · ${item.actor}${item.note ? ` · ${item.note}` : ""}</p></div><span class="audit-time">已记录</span></div>`).join(""); };
  const loadAudit = async () => { if (!state.ticketId) return toast("请先启动一笔演示工单", true); try { const ticketAudit = await api(`/api/audit?entity_id=${encodeURIComponent(state.ticketId)}`); const operationAudit = state.operation ? await api(`/api/audit?entity_id=${encodeURIComponent(state.operation.operation_id)}`) : []; state.audit = [...ticketAudit, ...operationAudit]; renderAudit(); } catch (error) { toast(error.message, true); } };
  const freshTicket = async (prefix) => api("/api/tickets", { method: "POST", body: JSON.stringify({ order_id: "ORD-1001", customer_id: "C1", request_type: "refund", reason: "商品破损，申请退款", reason_tags: ["damaged"], idempotency_key: `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}` }) });
  const freshSubmittedDraft = async (prefix) => { const ticket = await freshTicket(prefix); const draft = await api(`/api/tickets/${ticket.ticket_id}/refund-drafts`, { method: "POST", body: JSON.stringify({ amount: "200.00", reason_detail: "固定演示政策命中，金额由领域服务校验", idempotency_key: `${prefix}-draft-${Date.now()}-${Math.random().toString(16).slice(2)}` }) }); const submitted = await api(`/api/operations/${draft.operation_id}/submit`, { method: "POST" }); return { ticket, operation: submitted }; };
  const resetDemo = async () => { await api("/api/demo/reset", { method: "POST", body: "{}" }); state.ticketId = null; state.operation = null; state.audit = []; $("#audit-list").className = "audit-list empty-state"; $("#audit-list").innerHTML = '<span class="empty-icon">⌁</span><p>业务动作发生后，审计记录会显示在这里。</p>'; $("#ticket-value").textContent = "尚未创建"; $("#ticket-note").textContent = "案件状态摘要，不是 Agent"; $("#amount-value").textContent = "--"; $("#amount-note").textContent = "等待规则与政策校验"; $("#case-status").textContent = "待发起"; $("#status-note").textContent = "业务状态摘要，不是 Agent"; setStep(0, "待处理"); setButtons(); };
  const runScenario = async (name, action) => { const status = $("#scenario-status"); const result = $("#scenario-result"); document.querySelectorAll(".scenario-option").forEach((node) => { node.disabled = true; }); status.textContent = "运行中"; status.className = "status-tag neutral"; result.className = "scenario-result running"; result.innerHTML = "<span>正在重置合成数据并调用真实 API...</span>"; try { await resetDemo(); const outcome = await action(); status.textContent = "已验证"; status.className = "status-tag safe"; result.className = "scenario-result success"; result.innerHTML = `<strong>${escapeHtml(name)}</strong>${outcome.map((line) => `<p>${escapeHtml(line)}</p>`).join("")}`; toast(`${name}演示完成。`); await loadAudit(); } catch (error) { status.textContent = "执行失败"; status.className = "status-tag neutral"; result.className = "scenario-result blocked"; result.innerHTML = `<strong>${escapeHtml(name)}</strong><p>${escapeHtml(error.message)}</p>`; toast(error.message, true); } finally { document.querySelectorAll(".scenario-option").forEach((node) => { node.disabled = false; }); } };
  const runRejectScenario = () => runScenario("审批拒绝", async () => { const { operation } = await freshSubmittedDraft("ui-reject"); const rejected = await api(`/api/operations/${operation.operation_id}/reject`, { method: "POST", body: JSON.stringify({ reason: "凭证不足，转人工补充", expected_version: operation.version }) }, "demo-approver"); return [`操作 ${operation.operation_id}：${operation.status} → ${rejected.status}`, "副作用：0（未调用执行接口）", "审计：审批拒绝已记录"]; });
  const runUnknownScenario = () => runScenario("外部结果未知", async () => { const { operation } = await freshSubmittedDraft("ui-unknown"); const approved = await api(`/api/operations/${operation.operation_id}/approve`, { method: "POST", body: JSON.stringify({ reason: "演示授权", expected_version: operation.version }) }, "demo-approver"); const unknown = await api(`/api/operations/${operation.operation_id}/execute`, { method: "POST", body: JSON.stringify({ external_result: "timeout" }) }, "demo-system"); const reconciled = await api(`/api/operations/${operation.operation_id}/reconcile`, { method: "POST", body: JSON.stringify({ result: "success" }) }, "demo-system"); return [`操作 ${operation.operation_id}：${approved.status} → ${unknown.status} → ${reconciled.status}`, "约束：只使用原 operation_id 对账，没有换键重试", "副作用：仅在对账成功后记账，审计全程可追溯"]; });
  const runIdempotencyScenario = () => runScenario("重复请求", async () => { const key = `ui-idem-${Date.now()}`; const payload = { order_id: "ORD-1001", customer_id: "C1", request_type: "refund", reason: "商品破损，申请退款", reason_tags: ["damaged"], idempotency_key: key }; const first = await api("/api/tickets", { method: "POST", body: JSON.stringify(payload) }); const second = await api("/api/tickets", { method: "POST", body: JSON.stringify(payload) }); return [`第一次：${first.ticket_id}`, `第二次：${second.ticket_id}`, `结论：${first.ticket_id === second.ticket_id ? "同键同载荷返回原结果，未创建重复工单" : "幂等校验异常"}`]; });
  const runPermissionScenario = () => runScenario("角色越权", async () => { const { operation } = await freshSubmittedDraft("ui-permission"); try { await api(`/api/operations/${operation.operation_id}/approve`, { method: "POST", body: JSON.stringify({ reason: "越权尝试", expected_version: operation.version }) }, "demo-agent"); throw new Error("越权请求意外成功"); } catch (error) { if (error.status !== 403) throw error; return [`Agent 尝试审批：HTTP ${error.status}`, "结论：权限拒绝，操作仍保持 pending_approval", "副作用：0（领域服务未发生状态迁移）"]; } });
  const createTicket = async () => {
    if (state.ticketId) return toast("演示工单已创建，可继续下一步。");
    try {
      const key = `ui-ticket-${Date.now()}`;
      const ticket = await api("/api/tickets", { method: "POST", body: JSON.stringify({ order_id: "ORD-1001", customer_id: "C1", request_type: "refund", reason: "商品破损，申请退款", reason_tags: ["damaged"], idempotency_key: key }) });
      state.ticketId = ticket.ticket_id;
      $("#ticket-value").textContent = ticket.ticket_id.slice(0, 12) + "…";
      $("#ticket-note").textContent = "ORD-1001 · 破损退款申请";
      $("#flow-state").textContent = "工单已受理"; $("#flow-state").className = "status-tag safe";
      setStep(1, "可以生成草稿"); setButtons({ draft: true }); toast("工单已创建。下一步生成退款草稿。");
      await loadAudit();
    } catch (error) { toast(error.message, true); }
  };
  const createDraft = async () => {
    if (!state.ticketId) return createTicket();
    try {
      const draft = await api(`/api/tickets/${state.ticketId}/refund-drafts`, { method: "POST", body: JSON.stringify({ amount: "200.00", reason_detail: "订单已签收，破损原因命中演示政策 P-DEMO", idempotency_key: `ui-draft-${Date.now()}` }) });
      const submitted = await api(`/api/operations/${draft.operation_id}/submit`, { method: "POST" });
      setOperation(submitted); $("#flow-state").textContent = "等待审批"; setStep(2, "等待审批"); setButtons({ approve: true }); toast("退款草稿已提交，等待授权审批。"); await loadAudit();
    } catch (error) { toast(error.message, true); }
  };
  const approve = async () => {
    if (!state.operation) return toast("请先生成退款草稿", true);
    try {
      const approved = await api(`/api/operations/${state.operation.operation_id}/approve`, { method: "POST", body: JSON.stringify({ reason: "演示审批通过", expected_version: state.operation.version }) }, "demo-approver");
      setOperation(approved); $("#flow-state").textContent = "批准执行"; setStep(3, "可以执行"); setButtons({ execute: true }); toast("审批已通过。仅系统角色可以执行退款。"); await loadAudit();
    } catch (error) { toast(error.message, true); }
  };
  const execute = async () => {
    if (!state.operation) return toast("请先完成审批", true);
    try {
      const completed = await api(`/api/operations/${state.operation.operation_id}/execute`, { method: "POST", body: JSON.stringify({ external_result: "success" }) }, "demo-system");
      setOperation(completed); $("#flow-state").textContent = "处置完成"; setStep(4, "已完成"); setButtons(); toast("退款执行完成，审计轨迹已更新。"); await loadAudit();
    } catch (error) { toast(error.message, true); }
  };
  const checkApi = async () => { try { await fetch("/health/live").then((r) => { if (!r.ok) throw new Error(); }); const node = $("#api-status"); node.classList.add("online"); node.innerHTML = "<i></i> 服务运行正常"; } catch { $("#api-status").textContent = "服务不可用"; } };
  const renderTrace = (result) => {
    const status = $("#trace-run-status");
    const outcomeText = result.waiting_approval ? "已生成待审批草稿" : "已安全停止并转人工";
    status.textContent = outcomeText;
    status.className = `status-tag ${result.waiting_approval ? "safe" : "neutral"}`;
    $("#trace-title").textContent = result.mode_label;
    $("#trace-summary").textContent = `${outcomeText}。${result.isolation}；${result.side_effect ? "检测到副作用" : "未发生退款副作用"}。`;
    $("#trace-list").className = "trace-list";
    $("#trace-list").innerHTML = result.traces.map((trace, index) => {
      const statusClass = ["error", "escalated", "skipped"].includes(trace.status) ? trace.status : "ok";
      const tools = trace.tool_calls.length ? `<div class="trace-tools">${trace.tool_calls.map((tool) => `<span>${escapeHtml(tool)}</span>`).join("")}</div>` : "";
      const citations = trace.citations.length ? `<div class="trace-tools">${trace.citations.map((citation) => `<span>${escapeHtml(citation)}</span>`).join("")}</div>` : "";
      const reject = trace.reject_reason ? `<div class="trace-reject">停止原因：${escapeHtml(trace.reject_reason)}</div>` : "";
      const duration = typeof trace.duration_ms === "number" ? `${trace.duration_ms.toFixed(2)} ms` : "流程节点";
      return `<article class="trace-item"><span class="trace-icon ${statusClass}">${String(index + 1).padStart(2, "0")}</span><div><div class="trace-head"><strong>${escapeHtml(trace.agent_name)}</strong><small>${escapeHtml(duration)}</small></div><p class="trace-role">${escapeHtml(trace.role)}</p><p class="trace-output">${escapeHtml(trace.output_summary)}</p>${tools}${citations}${reject}</div></article>`;
    }).join("");
  };
  const runLab = async () => {
    const runButton = $("#run-lab");
    runButton.disabled = true; runButton.textContent = "正在运行隔离沙箱…";
    try { const result = await api("/api/agent-lab/trace", { method: "POST", body: JSON.stringify({ mode: state.labMode }) }); renderTrace(result); toast(result.waiting_approval ? "轨迹完成：已停在人工审批前。" : "轨迹已安全停止，原因已展示。"); }
    catch (error) { toast(error.message, true); }
    finally { runButton.disabled = false; runButton.innerHTML = '<span class="button-symbol">▶</span>运行当前轨迹'; }
  };
  const retrieveEvidence = async () => {
    const query = $("#retrieval-query").value.trim();
    if (!query) return toast("请输入要检索的政策问题", true);
    const button = $("#retrieve-evidence"); const status = $("#retrieval-status");
    button.disabled = true; status.textContent = "正在检索"; status.className = "status-tag neutral";
    try {
      const result = await api("/api/agent-lab/retrieve", { method: "POST", body: JSON.stringify({ query }) });
      if (result.status === "blocked") {
        status.textContent = "已拒绝"; status.className = "status-tag neutral";
        $("#retrieval-results").innerHTML = `<div class="retrieval-blocked"><strong>${escapeHtml(result.reason)}</strong><br>${escapeHtml(result.message)}</div>`;
        return;
      }
      if (result.status === "no_evidence") {
        status.textContent = "证据不足"; status.className = "status-tag neutral";
        $("#retrieval-results").innerHTML = `<div class="retrieval-blocked"><strong>${escapeHtml(result.reason)}</strong><br>${escapeHtml(result.message)}</div>`;
        return;
      }
      status.textContent = `命中 ${result.results.length} 条`; status.className = "status-tag safe";
      $("#retrieval-results").innerHTML = result.results.map((item) => `<article class="retrieval-result"><strong>${escapeHtml(item.citation)}</strong><p>${escapeHtml(item.text)}</p><div class="retrieval-meta"><span>融合分数 ${Number(item.score).toFixed(3)}</span><span>关键词命中 ${item.keyword_hits}</span><span>${item.citation_valid ? "引用已校验" : "引用无效"}</span></div></article>`).join("");
    } catch (error) { status.textContent = "检索失败"; toast(error.message, true); }
    finally { button.disabled = false; }
  };
  const runBoundaryScenario = async (name) => {
    const status = $("#boundary-status"); const output = $("#boundary-result");
    document.querySelectorAll(".boundary-option").forEach((button) => { button.disabled = true; });
    status.textContent = "运行中"; status.className = "status-tag neutral";
    output.className = "boundary-result running"; output.innerHTML = "<span>正在运行隔离工作流…</span>";
    try {
      const result = await api("/api/agent-lab/boundary-scenario", { method: "POST", body: JSON.stringify({ name }) });
      const details = [];
      if (result.error_code) details.push(`原因码：${result.error_code}`);
      if (result.questions?.length) details.push(`澄清问题：${result.questions.join("；")}`);
      if (result.before && result.after) details.push(`脱敏前：${result.before}`, `脱敏后：${result.after}`);
      details.push(`副作用：${result.side_effect ? "检测到副作用" : "0（未写入退款）"}`, result.evidence);
      status.textContent = "已验证"; status.className = "status-tag safe";
      output.className = "boundary-result success";
      output.innerHTML = `<strong>${escapeHtml(result.title)}</strong><p>结果：${escapeHtml(result.outcome || "安全停止")}</p>${details.map((detail) => `<p>${escapeHtml(detail)}</p>`).join("")}`;
      toast(`${result.title}演示完成。`);
    } catch (error) {
      status.textContent = "执行失败"; status.className = "status-tag neutral";
      output.className = "boundary-result blocked"; output.innerHTML = `<strong>演示未完成</strong><p>${escapeHtml(error.message)}</p>`;
      toast(error.message, true);
    } finally { document.querySelectorAll(".boundary-option").forEach((button) => { button.disabled = false; }); }
  };
  $("#start-case").addEventListener("click", createTicket); $("#draft-action").addEventListener("click", createDraft); $("#approve-action").addEventListener("click", approve); $("#execute-action").addEventListener("click", execute); $("#refresh-audit").addEventListener("click", loadAudit);
  document.querySelectorAll(".workspace-tab").forEach((tab) => tab.addEventListener("click", () => switchView(tab.dataset.viewTarget)));
  document.querySelectorAll("[data-switch-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.switchView)));
  document.querySelectorAll("[data-lab-mode]").forEach((button) => button.addEventListener("click", () => { state.labMode = button.dataset.labMode; document.querySelectorAll("[data-lab-mode]").forEach((node) => node.classList.toggle("selected", node === button)); }));
  $("#run-lab").addEventListener("click", runLab); $("#retrieve-evidence").addEventListener("click", retrieveEvidence);
  document.querySelectorAll("[data-scenario]").forEach((button) => button.addEventListener("click", () => ({ reject: runRejectScenario, unknown: runUnknownScenario, idempotency: runIdempotencyScenario, permission: runPermissionScenario }[button.dataset.scenario])()));
  document.querySelectorAll("[data-boundary]").forEach((button) => button.addEventListener("click", () => runBoundaryScenario(button.dataset.boundary)));
  $("#retrieval-query").addEventListener("keydown", (event) => { if (event.key === "Enter") retrieveEvidence(); });
  const flowButton = $("#show-flow");
  if (flowButton) flowButton.addEventListener("click", () => $("#flow").scrollIntoView({ behavior: "smooth" }));
  $("#open-help").addEventListener("click", () => $("#help-dialog").showModal()); $("#close-help").addEventListener("click", () => $("#help-dialog").close()); checkApi();
})();
