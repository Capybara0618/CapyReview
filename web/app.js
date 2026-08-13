const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const TITLES = {
  overview: "概览",
  review: "发起审查",
  tasks: "任务与 Trace",
  evolution: "反馈演化",
  evaluation: "离线评测",
};

let selectedTask = "";
let selectedTaskData = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function formatJson(value) {
  return JSON.stringify(value, null, 2);
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  const type = response.headers.get("content-type") || "";
  const data = type.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof data === "string" ? data : (data.detail || data.error || "请求失败");
    throw new Error(message);
  }
  return data;
}

function toast(message, tone = "info") {
  const root = $("#toast");
  root.textContent = message;
  root.dataset.tone = tone;
  root.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => root.classList.remove("show"), 2600);
}

function show(view) {
  const target = TITLES[view] ? view : "overview";
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${target}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === target));
  $("#page-title").textContent = TITLES[target];
  if (location.hash !== `#${target}`) history.replaceState(null, "", `#${target}`);
  if (target === "overview") loadOverview();
  if (target === "tasks") loadTasks();
  if (target === "evolution") loadEvolution();
  if (target === "evaluation") loadEvaluation();
}

function stateBadge(state) {
  const value = String(state || "PENDING").toUpperCase();
  return `<span class="state state-${value.toLowerCase()}">${escapeHtml(value)}</span>`;
}

function taskRows(tasks) {
  if (!tasks?.length) return '<div class="empty-state">暂无运行记录。</div>';
  return tasks.map((task) => `
    <button class="task-row" data-task="${escapeHtml(task.id)}" type="button">
      <span><strong>${escapeHtml(task.repository || "未命名仓库")}</strong>
      <small>${task.pull_request ? `PR #${escapeHtml(task.pull_request)}` : "手动审查"} · ${escapeHtml(formatTime(task.created_at))}</small></span>
      ${stateBadge(task.state)}
    </button>`).join("");
}

function bindTaskRows(root) {
  $$("[data-task]", root).forEach((row) => row.addEventListener("click", () => openTask(row.dataset.task)));
}

async function loadOverview() {
  try {
    const data = await api("/api/dashboard");
    const stats = data.stats || {};
    $("#system-status").textContent = `${data.queue} · ${data.llm?.model || "LLM"}`;
    const cards = [
      ["总任务", stats.tasks_total ?? 0, "累计进入 Runtime"],
      ["已完成", stats.tasks_success ?? 0, "生成结构化报告"],
      ["失败", stats.tasks_failed ?? 0, "可查看 Trace 并续跑"],
    ];
    $("#stats").innerHTML = cards.map(([label, value, note]) => `
      <article class="stat"><span>${label}</span><strong>${escapeHtml(value)}</strong><small>${note}</small></article>`).join("");
    $("#recent-tasks").innerHTML = taskRows((data.tasks || []).slice(0, 6));
    bindTaskRows($("#recent-tasks"));
  } catch (error) {
    $("#system-status").textContent = "服务不可用";
    $("#stats").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

async function loadTasks() {
  const root = $("#all-tasks");
  try {
    const data = await api("/api/tasks?limit=100");
    root.innerHTML = taskRows(data.tasks || []);
    bindTaskRows(root);
  } catch (error) {
    root.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function renderFindings(findings) {
  if (!findings.length) return '<div class="empty-state success">没有通过证据门禁的可执行问题。</div>';
  return `<div class="findings">${findings.map((finding) => `
    <article class="finding">
      <div>${stateBadge(finding.severity)}<strong>${escapeHtml(finding.title || finding.rule_id)}</strong></div>
      <code>${escapeHtml(finding.path)}:${escapeHtml(finding.line)}</code>
      <p>${escapeHtml(finding.explanation || "")}</p>
      <blockquote>${escapeHtml(finding.evidence || "")}</blockquote>
    </article>`).join("")}</div>`;
}

function renderTrace(trace) {
  if (!trace.length) return '<div class="empty-state">暂无 Trace。</div>';
  return `<ol class="trace">${trace.map((event) => `
    <li><b>${escapeHtml(event.step)}</b><span><strong>${escapeHtml(event.state || event.kind || "EVENT")}</strong>
    <small>${escapeHtml(event.message || event.detail || "")}</small></span></li>`).join("")}</ol>`;
}

function renderTask(task) {
  const report = task.report || {};
  const findings = report.findings || [];
  const collaboration = report.collaboration || {};
  $("#task-detail").innerHTML = `
    <div class="task-summary">
      <div>${stateBadge(task.state)}<h3>${escapeHtml(task.repository)}</h3><p>${escapeHtml(report.summary || task.error || "任务仍在执行")}</p></div>
      <dl><div><dt>风险</dt><dd>${escapeHtml(report.risk || "-")}</dd></div><div><dt>路由</dt><dd>${escapeHtml(collaboration.route || "-")}</dd></div><div><dt>Finding</dt><dd>${findings.length}</dd></div></dl>
    </div>
    <section><h4>结构化 Findings</h4>${renderFindings(findings)}</section>
    <section><h4>Run Trace</h4>${renderTrace(task.trace || [])}</section>
    <section><h4>协作摘要</h4><pre>${escapeHtml(formatJson(collaboration))}</pre></section>`;
  $("#task-raw").textContent = formatJson(task);
  $("#cancel-task").classList.toggle("hidden", ["SUCCESS", "FAILED", "CANCELLED"].includes(task.state));
  $("#resume-task").classList.toggle("hidden", !["FAILED", "CANCELLED"].includes(task.state));
  $("#feedback-panel").classList.toggle("hidden", !(task.state === "SUCCESS" && task.report));
  $("#feedback-finding").innerHTML = '<option value="">任务级反馈</option>' + findings.map((item, index) =>
    `<option value="${index}">${escapeHtml(item.rule_id)} · ${escapeHtml(item.path)}:${escapeHtml(item.line)}</option>`
  ).join("");
}

async function openTask(taskId) {
  show("tasks");
  selectedTask = taskId;
  $("#task-detail").innerHTML = '<div class="skeleton row"></div>';
  try {
    selectedTaskData = await api(`/v1/tasks/${encodeURIComponent(taskId)}`);
    renderTask(selectedTaskData);
    await loadFeedback(taskId);
  } catch (error) {
    $("#task-detail").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

async function loadFeedback(taskId) {
  const root = $("#feedback-history");
  try {
    const data = await api(`/v1/tasks/${encodeURIComponent(taskId)}/feedback`);
    root.innerHTML = (data.cases || []).length ? (data.cases || []).map((item) => `
      <div class="history-row"><strong>${escapeHtml(item.category)}</strong><span>${escapeHtml((item.payload || {}).note || "")}</span></div>`).join("") : "";
  } catch (error) {
    root.textContent = error.message;
  }
}

async function loadEvolution() {
  try {
    const [status, runs, versions] = await Promise.all([
      api("/v1/evolution/status"), api("/v1/evolution/runs?limit=10"),
      api("/v1/skills/review-auth-security/versions"),
    ]);
    $("#evolution-status").textContent = formatJson(status);
    const versionRows = (versions.versions || []).map((version) => `
      <div class="history-row"><strong>Skill V${escapeHtml(version.version)} ${version.active ? "· ACTIVE" : ""}</strong>
      ${version.active ? '<span>当前版本</span>' : `<button class="link" type="button" data-activate-version="${escapeHtml(version.version)}">回滚至此版本</button>`}</div>`).join("");
    const runRows = (runs.runs || []).map((run) => `
      <div class="history-row"><strong>V${escapeHtml(run.candidate_version || "-")} · ${escapeHtml(run.decision)}</strong>
      <span>${escapeHtml(formatTime(run.created_at))} · ${escapeHtml(run.candidate_score ?? "-")} / ${escapeHtml(run.baseline_score ?? "-")}</span></div>`).join("");
    $("#evolution-runs").innerHTML = versionRows + runRows || '<div class="empty-state">暂无演化记录。</div>';
    $$('[data-activate-version]', $("#evolution-runs")).forEach((button) => {
      button.addEventListener("click", () => activatePromptVersion(button.dataset.activateVersion));
    });
  } catch (error) {
    $("#evolution-status").textContent = error.message;
  }
}

async function activatePromptVersion(version) {
  try {
    await api(`/v1/skills/review-auth-security/versions/${encodeURIComponent(version)}/activate`, {
      method: "POST", body: "{}",
    });
    toast(`已激活 Skill V${version}`, "success");
    await loadEvolution();
  } catch (error) { toast(error.message, "error"); }
}

function percent(value) {
  return `${(Number(value || 0) * 100).toFixed(1)}%`;
}

async function loadEvaluation() {
  const root = $("#evaluation-summary");
  try {
    const data = await api("/api/evaluation");
    const dataset = data.dataset || {};
    if (data.status === "not_run") {
      root.innerHTML = `
        <article class="panel metric-panel"><span class="eyebrow">CONTROLLED DATASET</span><h3>${escapeHtml(dataset.cases || 0)} 条 PR Diff</h3>
          <dl><div><dt>风险</dt><dd>${escapeHtml(dataset.risk_cases || 0)}</dd></div><div><dt>干净</dt><dd>${escapeHtml(dataset.clean_cases || 0)}</dd></div><div><dt>仓库</dt><dd>${escapeHtml(dataset.repositories || 0)}</dd></div></dl></article>
        <article class="panel"><span class="eyebrow">STATUS</span><h3>尚未运行 LLM 评测</h3>
          <p>运行一次正式评测后，这里会读取最新完整报告；未完成的中间结果不会被展示。</p></article>
        <article class="panel limitations"><span class="eyebrow">LIMITATIONS</span><h3>实验边界</h3><ul>${(data.limitations || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></article>`;
      return;
    }
    const result = data.result || {};
    const metricRows = `
      <tr class="highlight"><td>CapyReview</td><td>${percent(result.f1)}</td><td>${percent(result.high_risk_recall)}</td><td>${percent(result.clean_accuracy)}</td></tr>`;
    root.innerHTML = `
      <article class="panel metric-panel"><span class="eyebrow">CONTROLLED DATASET</span><h3>${escapeHtml(dataset.cases || 0)} 条 PR Diff</h3>
        <dl><div><dt>风险</dt><dd>${escapeHtml(dataset.risk_cases || 0)}</dd></div><div><dt>干净</dt><dd>${escapeHtml(dataset.clean_cases || 0)}</dd></div><div><dt>仓库</dt><dd>${escapeHtml(dataset.repositories || 0)}</dd></div></dl></article>
      <article class="panel"><div class="panel-head"><div><span class="eyebrow">METRICS</span><h3>本次评测</h3></div></div>
        <table><thead><tr><th>方案</th><th>F1</th><th>高风险召回</th><th>干净准确率</th></tr></thead><tbody>
          ${metricRows}
        </tbody></table></article>
      <article class="panel limitations"><span class="eyebrow">LIMITATIONS</span><h3>实验边界</h3><ul>${(data.limitations || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></article>`;
  } catch (error) {
    root.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

$$('[data-view]').forEach((button) => button.addEventListener("click", () => show(button.dataset.view)));
$$('[data-jump]').forEach((button) => button.addEventListener("click", () => show(button.dataset.jump)));

$("#refresh").addEventListener("click", () => show((location.hash || "#overview").slice(1)));

const reviewForm = $("#review-form");
const diffInput = reviewForm.elements.diff;
diffInput.addEventListener("input", () => {
  const lines = diffInput.value ? diffInput.value.split("\n").length : 0;
  $("#diff-stats").textContent = `${lines} 行 · ${diffInput.value.length} 字符`;
});

$("#load-demo").addEventListener("click", () => {
  reviewForm.elements.repository.value = "demo/api";
  diffInput.value = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,3 @@\n def run(value):\n-    return value\n+    result = eval(value)\n+    return result\n";
  diffInput.dispatchEvent(new Event("input"));
});

reviewForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = new FormData(reviewForm);
  const payload = {
    repository: values.get("repository"), diff: values.get("diff"),
    pull_request: values.get("pull_request") ? Number(values.get("pull_request")) : null,
  };
  const target = $("#review-result");
  target.classList.remove("empty");
  target.textContent = "正在提交审查任务…";
  try {
    const query = values.get("async") ? "?async=true" : "";
    const data = await api(`/v1/reviews${query}`, { method: "POST", body: JSON.stringify(payload) });
    target.textContent = formatJson(data);
    toast("审查任务已创建", "success");
    if (data.task_id) window.setTimeout(() => openTask(data.task_id), values.get("async") ? 700 : 0);
  } catch (error) {
    target.textContent = error.message;
    toast(error.message, "error");
  }
});

$("#cancel-task").addEventListener("click", async () => {
  if (!selectedTask) return;
  try {
    await api(`/v1/tasks/${encodeURIComponent(selectedTask)}/cancel`, { method: "POST", body: "{}" });
    toast("已请求取消任务", "success");
    await openTask(selectedTask);
  } catch (error) { toast(error.message, "error"); }
});

$("#resume-task").addEventListener("click", async () => {
  if (!selectedTask) return;
  try {
    await api(`/v1/tasks/${encodeURIComponent(selectedTask)}/resume`, { method: "POST", body: "{}" });
    toast("任务已重新进入队列", "success");
    await openTask(selectedTask);
  } catch (error) { toast(error.message, "error"); }
});

$("#feedback-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedTask || !selectedTaskData?.report) return;
  const values = new FormData(event.currentTarget);
  const index = values.get("finding_index");
  const findings = selectedTaskData.report.findings || [];
  const payload = {
    category: values.get("category"), note: values.get("note"),
    finding: index === "" ? null : findings[Number(index)],
  };
  try {
    const data = await api(`/v1/tasks/${encodeURIComponent(selectedTask)}/feedback`, {
      method: "POST", body: JSON.stringify(payload),
    });
    $("#feedback-result").textContent = `${data.category} 已记录`;
    event.currentTarget.reset();
    await loadFeedback(selectedTask);
  } catch (error) { $("#feedback-result").textContent = error.message; }
});

$("#evolution-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = new FormData(event.currentTarget);
  const output = $("#evolution-result");
  output.classList.remove("empty");
  output.textContent = "正在执行 Validation / Holdout 门禁…";
  try {
    const data = await api("/v1/evolution/propose", {
      method: "POST", body: JSON.stringify({
        skill_name: values.get("skill_name"),
        package: JSON.parse(values.get("package")),
      }),
    });
    output.textContent = formatJson(data);
    await loadEvolution();
  } catch (error) { output.textContent = error.message; }
});

$("#auto-evolve").addEventListener("click", async () => {
  const output = $("#evolution-result");
  output.classList.remove("empty");
  output.textContent = "正在从确认反馈生成候选…";
  try {
    const data = await api("/v1/evolution/auto", {
      method: "POST", body: JSON.stringify({ skill_name: "review-auth-security" }),
    });
    output.textContent = formatJson(data);
    await loadEvolution();
  } catch (error) { output.textContent = error.message; }
});

window.addEventListener("hashchange", () => show((location.hash || "#overview").slice(1)));
show((location.hash || "#overview").slice(1));
