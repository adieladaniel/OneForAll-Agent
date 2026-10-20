/* OneForAll Agent dashboard. Vanilla JS, no build step.
 * All dynamic text goes through textContent / text nodes (see h()), so nothing from the agent or the
 * environment is ever interpreted as HTML. */
"use strict";

const $ = (id) => document.getElementById(id);
const TERMINAL = ["succeeded", "failed", "needs_attention", "cancelled"];
const LIVE_STATUSES = ["pending", "queued", "running", "waiting_approval", "waiting_user", "verifying"];
const NEUTRAL_ERRORS = ["denied", "skipped", "verification_failed", "finish_not_alone"];
const STATUS_LABEL = {
  idle: "Idle", pending: "Starting", queued: "Queued", running: "Running", waiting_approval: "Waiting for approval",
  waiting_user: "Waiting for your answer", verifying: "Verifying", succeeded: "Succeeded", failed: "Failed",
  needs_attention: "Needs attention", cancelled: "Cancelled",
};
const PILL_CLASS = {
  idle: "pill-idle", pending: "pill-running", queued: "pill-queued", running: "pill-running", waiting_approval: "pill-waiting",
  waiting_user: "pill-waiting", verifying: "pill-verifying", succeeded: "pill-succeeded", failed: "pill-failed",
  needs_attention: "pill-attention", cancelled: "pill-cancelled",
};

/* ------------------------------------------------------------------ DOM helpers */

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const ICONS = {
  tool: [["path", "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"]],
  browser: [["circle", 12, 12, 10], ["path", "M2 12h20"], ["path", "M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"]],
  file: [["path", "M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"], ["path", "M14 2v6h6"]],
  http: [["path", "M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"], ["path", "M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"]],
  think: [["path", "M7.9 20A9 9 0 1 0 4 16.1L2 22z"]],
  approval: [["path", "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"], ["path", "m9 12 2 2 4-4"]],
  question: [["circle", 12, 12, 10], ["path", "M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"], ["path", "M12 17h.01"]],
  guard: [["path", "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3z"], ["path", "M12 9v4"], ["path", "M12 17h.01"]],
  verify: [["circle", 12, 12, 10], ["path", "m9 12 2 2 4-4"]],
  plan: [["path", "M8 6h13"], ["path", "M8 12h13"], ["path", "M8 18h13"], ["path", "M3 6h.01"], ["path", "M3 12h.01"], ["path", "M3 18h.01"]],
  memory: [["path", "m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"]],
  finish: [["path", "M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"], ["path", "M4 22v-7"]],
};
const BROWSER_TOOLS = new Set(["navigate", "click", "type", "select", "press", "scroll", "go_back", "download", "screenshot", "find", "read_page", "browser_state", "view"]);

function svgIcon(name, cls) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  for (const part of ICONS[name] || ICONS.tool) {
    let el;
    if (part[0] === "circle") {
      el = document.createElementNS(ns, "circle");
      el.setAttribute("cx", part[1]); el.setAttribute("cy", part[2]); el.setAttribute("r", part[3]);
    } else {
      el = document.createElementNS(ns, "path");
      el.setAttribute("d", part[1]);
    }
    svg.append(el);
  }
  return h("span", { class: `icon icon-${cls || name}` }, svg);
}

function toolIconName(tool, kind) {
  if (kind === "ask_user" || tool === "ask_user") return ["question", "question"];
  if (tool === "update_plan") return ["plan", "plan"];
  if (tool === "remember") return ["memory", "plan"];
  if (tool === "finish") return ["finish", "finish"];
  if (tool === "http_request") return ["http", "tool"];
  if (tool.endsWith("_file") || tool === "list_files") return ["file", "tool"];
  if (BROWSER_TOOLS.has(tool) || tool.startsWith("browser")) return ["browser", "tool"];
  return ["tool", "tool"];
}

function fmtDuration(ms) {
  if (!ms) return "";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}

function fmtValue(v) {
  if (typeof v === "string") return v;
  try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
}

function kvTable(obj) {
  const entries = Object.entries(obj || {});
  if (!entries.length) return h("div", { class: "muted small" }, "(no arguments)");
  return h("table", { class: "kv" }, h("tbody", null, entries.map(([k, v]) => h("tr", null, h("td", null, k), h("td", null, fmtValue(v))))));
}

function fallbackAction(tool, args) {
  if (tool === "finish") return `Declare the task finished (status: ${(args && args.status) || "?"})`;
  if (tool === "update_plan") return `Update the plan (${((args && args.items) || []).length} items)`;
  const bits = Object.entries(args || {}).map(([k, v]) => {
    const s = fmtValue(v).replace(/\s+/g, " ");
    return `${k}=${s.length > 50 ? s.slice(0, 47) + "..." : s}`;
  });
  return bits.length ? bits.join(", ") : "";
}

const INTERNAL_TOOLS = new Set(["remember", "update_plan", "finish", "ask_user"]);

function riskTag(risk, tool) {
  if (tool && INTERNAL_TOOLS.has(tool)) return null; // bookkeeping tools: the risk badge would only be noise
  return risk ? h("span", { class: `tag tag-${risk}`, title: "Risk level of this action" }, risk) : null;
}

function fileUrl(runId, path) {
  return `/runs/${encodeURIComponent(runId)}/files/${path.split("/").map(encodeURIComponent).join("/")}`;
}
const isImage = (p) => /\.(png|jpe?g|gif|webp)$/i.test(p);

function toast(message, kind) {
  const el = h("div", { class: `toast ${kind || ""}` }, message);
  $("toasts").append(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
}

async function api(method, url, body) {
  const resp = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await resp.json(); } catch (e) { /* no body */ }
  if (resp.status === 401) {
    if (S.mode === "db") { showLogin("Your session has ended. Please sign in again."); throw new Error("Please sign in"); }
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
    throw new Error("Please sign in");
  }
  if (!resp.ok) {
    const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : resp.statusText;
    throw new Error(detail);
  }
  return data;
}

/* ------------------------------------------------------------------ state */

const S = {
  mode: "file", // "file" = v1 (shared password or none), "db" = multi-user (see detectMode)
  me: null, signedIn: false, runMeta: null, runIndex: new Map(), started: false,
  ws: null, connToken: 0, runId: null, finished: false, activeRunId: null,
  autoscroll: true, scrollQueued: false,
  view: null, // per-run rendering state, rebuilt by resetView()
  timer: null,
};

function freshView() {
  return {
    container: $("timeline"), // where new steps go (timeline, or the verification section body)
    runningCards: [], // {tool, el, update}
    pendingCard: null, pendingEntry: null,
    facts: new Map(), plan: [], status: "idle", startedAt: null,
    stats: { llm_calls: 0, input_tokens: 0, output_tokens: 0, llm_retries: 0, steps: 0, retries: 0, failures: 0, approved: 0, denied: 0 },
    verifyBody: null,
    actions: new Map(), live: null, replaying: false, // live browser card: latest screenshot + caption
  };
}

function resetView() {
  S.view = freshView();
  $("timeline").replaceChildren();
  $("pending").replaceChildren();
  $("goal").textContent = "";
  $("criteria-box").classList.add("hidden");
  $("plan").classList.remove("finished");
  renderPlan();
  renderMemory();
  renderStats();
  setStatus("idle");
  $("timeline-meta").textContent = "";
  renderLive();
  S.autoscroll = true;
  $("autoscroll-btn").classList.add("hidden");
}

/* ------------------------------------------------------------------ status, stats, memory, plan */

function setStatus(status) {
  S.view.status = status;
  const pill = $("run-pill");
  pill.className = `pill ${PILL_CLASS[status] || "pill-idle"}`;
  pill.textContent = STATUS_LABEL[status] || status;
  if (S.mode === "db") updateRunButtons();
}

function tickTimer() {
  const v = S.view;
  const meta = $("timeline-meta");
  if (!v || !v.startedAt || TERMINAL.includes(v.status) || v.status === "idle") return;
  const secs = Math.max(0, Math.floor((Date.now() - v.startedAt) / 1000));
  meta.textContent = `elapsed ${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(secs % 60).padStart(2, "0")}`;
}

function renderStats() {
  const s = S.view.stats;
  const items = [
    ["LLM calls", s.llm_calls], ["Tokens", (s.input_tokens + s.output_tokens).toLocaleString()],
    ["Steps", s.steps], ["Tool retries", s.retries], ["Failed steps", s.failures],
    ["Approvals", `${s.approved}${s.denied ? ` / ${s.denied} denied` : ""}`], ["LLM retries", s.llm_retries],
  ];
  $("stats").replaceChildren(...items.map(([k, v]) => h("div", { class: "stat" }, h("dt", null, k), h("dd", null, v))));
}

function renderMemory(freshKey) {
  const facts = [...S.view.facts.values()];
  const box = $("memory");
  if (!facts.length) {
    box.replaceChildren(h("span", { class: "muted small" }, "Nothing remembered yet."));
    return;
  }
  box.replaceChildren(h("table", null,
    h("thead", null, h("tr", null, h("th", null, "Key"), h("th", null, "Value"), h("th", null, "Source"))),
    h("tbody", null, facts.map((f) => h("tr", { class: f.key === freshKey ? "fresh" : "" },
      h("td", { class: "k" }, f.key, f.durable ? h("span", { class: "tag tag-ok durable-tag", title: "Candidate for the organization's memory: saved only if this run is independently verified" }, "durable") : null),
      h("td", { class: "v" }, f.value), h("td", { class: "s" }, f.source || ""))))));
}

const PLAN_MARK = { done: "✓", blocked: "!", skipped: "–", in_progress: "", pending: "" };

function renderPlan() {
  const plan = S.view.plan;
  const list = $("plan");
  if (!plan.length) {
    list.replaceChildren(h("li", { class: "plain" }, "The plan appears once the agent has one."));
    return;
  }
  list.replaceChildren(...plan.map((p) => h("li", { class: p.status },
    h("span", { class: "pmark" }, PLAN_MARK[p.status] || ""),
    h("span", { class: "ptext" }, p.text, p.note ? h("span", { class: "pnote" }, p.note) : null))));
}

/* ------------------------------------------------------------------ timeline pieces */

function scheduleScroll() {
  if (!S.autoscroll || S.scrollQueued) return;
  S.scrollQueued = true;
  requestAnimationFrame(() => {
    S.scrollQueued = false;
    const sc = $("scroller");
    sc.scrollTop = sc.scrollHeight;
  });
}

function append(el) {
  S.view.container.append(el);
  $("empty")?.remove();
  scheduleScroll();
  return el;
}

function noteRow(iconName, iconCls, text) {
  return append(h("div", { class: "note-row" }, svgIcon(iconName, iconCls), h("span", null, text)));
}

function thumbs(runId, evidence) {
  if (!evidence || !evidence.length) return null;
  return h("div", { class: "thumbs" }, evidence.map((p) => {
    const url = fileUrl(runId, p);
    if (isImage(p)) {
      return h("button", { class: "thumb", type: "button", title: p, onclick: () => openLightbox(url, p) },
        h("img", { src: url, alt: p, loading: "lazy" }), h("span", null, p.split("/").pop()));
    }
    return h("a", { class: "file-link", href: url, target: "_blank", rel: "noopener" }, p);
  }));
}

function buildToolCard(info) {
  // info: {tool, kind, action, args, risk, verify}
  const [iconName, iconCls] = toolIconName(info.tool, info.kind);
  const compact = info.tool === "update_plan" || info.tool === "remember";
  const spinner = h("span", { class: "spinner" });
  const stateTag = h("span", { class: "tag" }, "running");
  const badges = h("div", { class: "step-badges" });
  const head = h("div", { class: "step-head" },
    svgIcon(iconName, iconCls),
    h("div", { class: "step-main" }, h("div", { class: "step-title" },
      h("span", { class: "tool-name" }, info.tool || "step"),
      h("span", { class: "tool-action" }, (info.tool === "finish" || info.tool === "update_plan") ? fallbackAction(info.tool, info.args) : (info.action || fallbackAction(info.tool, info.args))))),
    badges, h("span", { class: "chev" }, "▶"));
  const body = h("div", { class: "step-body" });
  const card = h("div", { class: `step ${compact ? "compact" : ""}`, "data-state": "running" }, head);
  head.addEventListener("click", () => card.classList.toggle("open"));
  const setBadges = (list) => badges.replaceChildren(...list.filter(Boolean));
  setBadges([riskTag(info.verify ? null : info.risk, info.tool), spinner]);

  const update = (step, runId) => {
    const failed = step.ok === false;
    const neutral = failed && NEUTRAL_ERRORS.includes(step.error);
    card.dataset.state = failed ? (neutral ? "neutral" : "error") : "ok";
    stateTag.className = `tag ${failed ? (neutral ? "tag-warn" : "tag-err") : "tag-ok"}`;
    stateTag.textContent = failed ? (neutral ? step.error : "error") : "ok";
    setBadges([
      riskTag(info.verify ? null : (step.risk || info.risk), info.tool),
      step.retries ? h("span", { class: "tag tag-warn", title: "Automatic retries of transient failures" }, `↻ ${step.retries} ${step.retries === 1 ? "retry" : "retries"}`) : null,
      step.duration_ms ? h("span", { class: "tag tag-time" }, fmtDuration(step.duration_ms)) : null,
      stateTag,
    ]);
    card.querySelectorAll(".step-error, .step-note, .thumbs").forEach((n) => n.remove());
    if (failed) {
      const text = (step.observation || "").trim();
      const shown = text.length > 420 ? text.slice(0, 420) + "..." : text;
      const box = h("div", { class: neutral ? "step-note" : "step-error" },
        h("b", null, step.error || "error"), shown ? `  ${shown}` : "");
      card.insertBefore(box, body);
    }
    const t = thumbs(runId, step.evidence);
    if (t) card.insertBefore(t, body);
    body.replaceChildren(
      h("h4", null, "Arguments"), kvTable(step.args || info.args),
      h("h4", null, "Observation"), h("pre", { class: "obs" }, step.observation || "(empty)"));
    card.append(body);
  };
  card.append(body);
  body.append(h("h4", null, "Arguments"), kvTable(info.args));
  return { el: card, update };
}

function renderLive() {
  const live = S.view && S.view.live;
  $("live-btn").classList.toggle("hidden", !live);
  $("live-empty").classList.toggle("hidden", !!live);
  $("live-caption").textContent = live ? live.caption : "";
  if (live) $("live-img").src = live.url; else $("live-img").removeAttribute("src");
}

function noteLiveShot(p) {
  const shots = (p.evidence || []).filter(isImage);
  if (!shots.length) return;
  const v = S.view;
  const action = v.actions.get(p.tool) || p.action || fallbackAction(p.tool, p.args || {});
  v.live = { url: fileUrl(p.run_id || S.runId, shots[shots.length - 1]), caption: `Step ${v.stats.steps}: ${p.tool} ${action}`.trim() };
  if (!v.replaying) renderLive(); // while replaying history only the final screenshot is loaded (see replay_done)
}

function handleToolStarted(p) {
  S.view.actions.set(p.tool, p.action || fallbackAction(p.tool, p.args || {}));
  const card = buildToolCard({ tool: p.tool, action: p.action, args: p.args, risk: p.risk });
  S.view.runningCards.push({ tool: p.tool, ...card });
  append(card.el);
}

function handleToolFinished(p) {
  const v = S.view;
  const verify = p.phase === "verify";
  const stats = v.stats;
  if (!verify && (p.kind === "tool" || p.kind === "ask_user")) {
    stats.steps += 1; stats.retries += p.retries || 0;
    if (!p.ok && !NEUTRAL_ERRORS.includes(p.error)) stats.failures += 1;
  }
  if (!verify && p.kind === "system") return;
  const idx = v.runningCards.findIndex((c) => c.tool === p.tool);
  let card;
  if (idx >= 0) card = v.runningCards.splice(idx, 1)[0];
  else {
    card = buildToolCard({ tool: p.tool, kind: p.kind, args: p.args, risk: p.risk, verify });
    append(card.el);
  }
  card.update(p, p.run_id || S.runId);
  noteLiveShot(p);
  renderStats();
  scheduleScroll();
}

function showModel(model, unavailable) {
  const badge = $("model-badge");
  badge.textContent = `${badge.dataset.provider || "llm"} · ${model}`;
  if (unavailable && unavailable.length) badge.title = `Skipped today (quota used up or unavailable): ${unavailable.join(", ")}`;
}

function handleThought(p) {
  if (!p.text || !p.text.trim()) return;
  append(h("div", { class: "thought" }, p.text.trim()));
}

/* ------------------------------------------------------------------ approvals and questions */

function pendingTitle(text, iconName) {
  return h("div", { class: "pending-title" }, svgIcon(iconName, "approval"), text);
}

function contextBlock(ctx) {
  if (!ctx) return null;
  const parts = [];
  if (ctx.warning) {
    const unsourced = Array.isArray(ctx.unsourced_values) ? ctx.unsourced_values : [];
    parts.push(h("div", { class: "unsourced-warning", role: "alert" },
      h("div", { class: "unsourced-title" }, "Values with no source in this run"),
      h("p", null, String(ctx.warning)),
      unsourced.length ? h("ul", null, unsourced.map((u) => h("li", null, `${u.label}: ${u.value}`))) : null));
  }
  if (Array.isArray(ctx.form_values) && ctx.form_values.length) {
    parts.push(h("div", { class: "form-values" },
      h("div", { class: "form-values-title" }, "Values to be submitted"),
      h("table", { class: "kv" }, h("tbody", null, ctx.form_values.map((f) =>
        h("tr", null, h("td", null, f.label), h("td", { class: f.value ? "" : "muted" }, f.value || "(empty)")))))));
  }
  if (ctx.reason) parts.push(h("p", { class: "pending-reason" }, `Agent's reasoning: ${ctx.reason}`));
  if (ctx.facts && String(ctx.facts).trim() && !/^\(?no /i.test(String(ctx.facts).trim())) {
    parts.push(h("details", { class: "context" }, h("summary", null, "What the agent knows so far"), h("pre", null, String(ctx.facts))));
  }
  return parts.length ? parts : null;
}

async function respond(payload, buttons) {
  buttons.forEach((b) => (b.disabled = true));
  try {
    await api("POST", `/api/runs/${encodeURIComponent(S.runId)}/respond`, payload);
  } catch (err) {
    toast(`Could not send response: ${err.message}`, "error");
    buttons.forEach((b) => (b.disabled = false));
    if (S.view.gate) S.view.gate();
  }
}

const APPROVER_ROLES = ["approver", "admin", "owner"];

/* Who may answer this approval, and may the current user approve it? The server enforces the same rules
 * (worker/org_settings.py); this only explains them and disables the buttons. */
function approvalRights(risk) {
  if (S.mode !== "db" || !S.me) return { who: "", canApprove: true, canDeny: true, reason: "" };
  const policy = S.me.org.policy;
  const roles = APPROVER_ROLES.concat(policy.operators_can_approve ? ["operator"] : []);
  let who = `Can approve: ${roles.join(", ")}.`;
  if (policy.separation_of_duties) {
    who += ` Whoever started the run cannot approve its irreversible actions${policy.sod_for_writes ? " or writes" : ""}.`;
  }
  if (!S.me.capabilities.approve) {
    const hint = S.me.org.role === "operator" ? " An admin can allow operators to approve in the organization settings." : "";
    return { who, canApprove: false, canDeny: false, reason: `Your role (${S.me.org.role}) cannot answer approvals.${hint}` };
  }
  const starter = S.runMeta && S.runMeta.started_by && S.runMeta.started_by.id;
  const blocked = policy.separation_of_duties && starter === S.me.user.id && (risk === "irreversible" || (risk === "write" && policy.sod_for_writes));
  if (blocked) {
    return { who, canApprove: false, canDeny: true, reason: `Separation of duties: you started this run, so someone else must approve its ${risk} actions. You can still deny.` };
  }
  return { who, canApprove: true, canDeny: true, reason: "" };
}

function handleApprovalRequested(p) {
  const v = S.view;
  if (v.pendingRequestId === p.request_id) return; // the same request again: its worker was replaced and the run re-attached
  v.pendingRequestId = p.request_id;
  const note = h("input", { type: "text", placeholder: "Optional note (e.g. reason for denying)" });
  const approve = h("button", { class: "btn btn-approve", type: "button" }, "Approve");
  const similar = h("button", { class: "btn btn-approve-soft", type: "button", title: "Approve this and skip the prompt for similar actions in this run" }, "Approve all similar");
  const deny = h("button", { class: "btn btn-deny", type: "button" }, "Deny");
  const buttons = [approve, similar, deny];
  approve.addEventListener("click", () => respond({ request_id: p.request_id, approved: true, note: note.value }, buttons));
  similar.addEventListener("click", () => respond({ request_id: p.request_id, approved: true, remember: true, note: note.value }, buttons));
  deny.addEventListener("click", () => respond({ request_id: p.request_id, approved: false, note: note.value }, buttons));
  const rightsBox = h("div", { class: "rights-note small hidden" });
  const gate = () => { // re-run when the run's starter becomes known
    const rights = approvalRights(p.risk);
    approve.disabled = !rights.canApprove;
    similar.disabled = !rights.canApprove || p.risk === "irreversible";
    deny.disabled = !rights.canDeny;
    note.disabled = !rights.canDeny;
    similar.title = p.risk === "irreversible" ? "Irreversible actions are always asked individually" : "Approve this and skip the prompt for similar actions in this run";
    rightsBox.replaceChildren(rights.reason ? h("div", { class: "rights-reason" }, rights.reason) : null, rights.who ? h("div", { class: "muted" }, rights.who) : null);
    rightsBox.classList.toggle("hidden", !rights.reason && !rights.who);
  };
  v.gate = gate;
  gate();
  const card = h("div", { class: "pending-card" },
    pendingTitle("Approval needed", "approval"),
    h("div", null, riskTag(p.risk), " ", h("span", { class: "muted small" }, `tool: ${p.tool}`)),
    h("div", { class: "pending-action" }, p.action || p.tool),
    kvTable(p.args), contextBlock(p.context), rightsBox,
    h("div", { class: "pending-buttons" }, approve, similar, deny, note));
  $("pending").replaceChildren(card);
  v.pendingCard = card;
  const entry = h("div", { class: "step", "data-state": "running" },
    h("div", { class: "step-head" }, svgIcon("approval", "approval"),
      h("div", { class: "step-main" }, h("div", { class: "step-title" }, h("span", { class: "tool-name" }, "approval"), h("span", { class: "tool-action" }, p.action || p.tool))),
      h("div", { class: "step-badges" }, riskTag(p.risk), h("span", { class: "tag tag-warn" }, "waiting for you"))));
  v.pendingEntry = { el: entry, action: p.action || p.tool, risk: p.risk, tool: p.tool, args: p.args };
  append(entry);
  setStatus("waiting_approval");
  document.title = "⚠ Approval needed - OneForAll Agent";
}

function handleApprovalResolved(p) {
  const v = S.view;
  $("pending").replaceChildren();
  v.pendingCard = null;
  v.gate = null;
  v.pendingRequestId = null;
  document.title = "OneForAll Agent";
  const resp = p.response || {};
  const approved = !!resp.approved;
  const e = v.pendingEntry;
  v.pendingEntry = null;
  if (approved) v.stats.approved += 1; else v.stats.denied += 1;
  renderStats();
  if (e) {
    e.el.dataset.state = approved ? "ok" : "neutral";
    const badges = e.el.querySelector(".step-badges");
    const by = resp.by && (resp.by.name || resp.by.email) ? ` by ${resp.by.name || resp.by.email}` : "";
    const label = !p.answered ? (p.reason || "no response") : approved ? (resp.remember ? `approved${by} (similar too)` : `approved${by}`) : `denied${by}`;
    badges.replaceChildren(riskTag(e.risk), h("span", { class: `tag ${approved ? "tag-ok" : "tag-warn"}` }, label));
    if (resp.note) e.el.append(h("div", { class: "step-note" }, `Note: ${resp.note}`));
  }
  if (!TERMINAL.includes(v.status)) setStatus("running");
}

function handleQuestionAsked(p) {
  const v = S.view;
  const answer = h("input", { type: "text", placeholder: "Type your answer..." });
  const send = h("button", { class: "btn btn-approve", type: "button" }, "Send answer");
  const options = (p.options || []).map((o) => h("button", { class: "btn btn-option", type: "button" }, o));
  const buttons = [send, ...options];
  const mayAnswer = S.mode !== "db" || !S.me || S.me.capabilities.answer;
  if (!mayAnswer) { buttons.forEach((b) => (b.disabled = true)); answer.disabled = true; answer.placeholder = "Viewers cannot answer questions"; }
  options.forEach((b) => b.addEventListener("click", () => respond({ request_id: p.request_id, answer: b.textContent }, buttons)));
  const submit = () => { if (answer.value.trim()) respond({ request_id: p.request_id, answer: answer.value.trim() }, buttons); };
  send.addEventListener("click", submit);
  answer.addEventListener("keydown", (ev) => { if (ev.key === "Enter") submit(); });
  const card = h("div", { class: "pending-card" },
    pendingTitle("The agent has a question", "question"),
    h("div", { class: "pending-action" }, p.question),
    options.length ? h("div", { class: "option-row" }, options) : null,
    h("div", { class: "answer-row" }, answer, send));
  $("pending").replaceChildren(card);
  v.pendingCard = card;
  setStatus("waiting_user");
  document.title = "❓ Question - OneForAll Agent";
  if (mayAnswer) setTimeout(() => answer.focus(), 50);
}

function handleQuestionAnswered() {
  $("pending").replaceChildren();
  S.view.pendingCard = null;
  document.title = "OneForAll Agent";
  if (!TERMINAL.includes(S.view.status)) setStatus("running");
}

/* ------------------------------------------------------------------ verification and report */

function handleVerificationStarted() {
  const v = S.view;
  v.container = $("timeline");
  const body = h("div", { class: "verify-body" });
  const section = h("div", { class: "verify-section" },
    h("div", { class: "verify-head" }, svgIcon("verify", "verify"), "Independent verification",
      h("small", null, "a fresh-context auditor re-checks the real system with read-only tools")),
    body);
  append(section);
  v.verifyBody = body;
  v.container = body;
  setStatus("verifying");
}

function checksList(checks) {
  return h("ul", { class: "checks" }, (checks || []).map((c) => h("li", null,
    h("span", { class: `mark ${c.passed ? "pass" : "fail"}` }, c.passed ? "✓" : "✗"),
    h("span", null, c.criterion, h("span", { class: "observed" }, c.observed || "")))));
}

function handleVerificationFinished(p) {
  const v = S.view;
  const kind = p.inconclusive ? "inconclusive" : p.passed ? "passed" : "failed";
  const label = { passed: "Verification PASSED", failed: "Verification FAILED", inconclusive: "Verification INCONCLUSIVE" }[kind];
  const result = h("div", { class: `verify-result ${kind}` },
    h("div", { class: "verify-verdict" }, label), p.summary ? h("div", null, p.summary) : null, checksList(p.checks));
  (v.verifyBody || v.container).append(result);
  v.container = $("timeline");
  v.verifyBody = null;
  if (!TERMINAL.includes(v.status)) setStatus("running");
  scheduleScroll();
}

const BANNERS = {
  succeeded: ["✓", "Task completed and independently verified"],
  failed: ["✗", "Task failed"],
  needs_attention: ["!", "Needs your attention"],
  cancelled: ["–", "Run cancelled"],
};

function handleRunFinished(p) {
  const v = S.view;
  v.container = $("timeline");
  $("pending").replaceChildren();
  document.title = "OneForAll Agent";
  setStatus(p.status);
  $("plan").classList.add("finished");
  S.finished = true;
  if (S.activeRunId === S.runId) S.activeRunId = null;
  updateRunButtons();
  const r = p.report || {};
  const [mark, title] = BANNERS[p.status] || ["?", p.status];
  const runId = p.run_id || S.runId;
  const quotaOut = /out of free-tier quota|quota exhausted/i.test(r.summary || "");
  const card = h("div", { class: `report ${p.status}` },
    quotaOut ? h("div", { class: "notice" }, "All Gemini models are out of free-tier quota for today. The demo shares one API key, so this run stopped early. Try again tomorrow (the quota resets daily), or pick a task that needs fewer model calls.") : null,
    h("div", { class: "report-banner" }, h("span", { class: "banner-mark" }, mark), title),
    h("div", { class: "report-section" }, h("h3", null, "Summary"), h("div", { class: "report-summary" }, r.summary || "(no summary)")));
  const outputs = Object.entries(r.outputs || {});
  if (outputs.length) {
    card.append(h("div", { class: "report-section" }, h("h3", null, "Outputs"),
      h("table", { class: "kv" }, h("tbody", null, outputs.map(([k, val]) => h("tr", null, h("td", null, k), h("td", null, fmtValue(val))))))));
  }
  const ver = r.verification || {};
  const verTitle = !ver.ran ? "Independent verification: not run" :
    `Independent verification: ${ver.inconclusive ? "INCONCLUSIVE" : ver.passed ? "PASSED" : "FAILED"}`;
  card.append(h("div", { class: "report-section" }, h("h3", null, verTitle),
    ver.summary ? h("div", { class: "small muted" }, ver.summary) : null, ver.checks && ver.checks.length ? checksList(ver.checks) : null));
  if ((r.evidence || []).length) {
    card.append(h("div", { class: "report-section" }, h("h3", null, "Agent's evidence"),
      h("ul", { class: "evidence-list" }, r.evidence.map((e) => h("li", null, e.claim, " ", h("span", { class: "src" }, `(source: ${e.source})`))))));
  }
  const artifacts = r.artifacts || [];
  if (artifacts.length) {
    card.append(h("div", { class: "report-section" }, h("h3", null, "Evidence files"),
      thumbs(runId, artifacts.filter(isImage)) || null,
      h("div", { class: "report-links" }, artifacts.filter((a) => !isImage(a)).map((a) =>
        h("a", { href: fileUrl(runId, a), target: "_blank", rel: "noopener" }, a)))));
  }
  const st = r.stats || {};
  card.append(h("div", { class: "report-section" },
    h("div", { class: "report-stats" }, Object.entries(st).map(([k, val]) => h("span", { class: "tag" }, `${k.replace(/_/g, " ")}: ${val}`))),
    h("div", { class: "report-links", style: null }, h("a", { href: fileUrl(runId, "report.md"), target: "_blank", rel: "noopener" }, "Open report.md"))));
  append(card);
  if (quotaOut && !v.replaying) toast("All Gemini models are out of quota for today. Try again tomorrow.", "error");
  refreshHistory();
}

/* ------------------------------------------------------------------ event dispatch */

function handleEvent(ev) {
  const p = ev.payload || {};
  const v = S.view;
  switch (ev.type) {
    case "run_started": {
      v.startedAt = Date.parse(ev.ts) || Date.now();
      setStatus("running");
      $("empty")?.remove();
      append(h("div", { class: "task-card" }, h("div", { class: "label" }, "Task"), h("p", null, p.task),
        h("div", { class: "muted small" }, `approval mode: ${(p.approval_mode || "").replace(/_/g, " ")}  ·  run ${S.runId.slice(0, 8)}`)));
      break;
    }
    case "thought": handleThought(p); break;
    case "plan_updated": {
      v.plan = p.plan || [];
      if (p.goal_summary) $("goal").textContent = p.goal_summary;
      if (p.success_criteria && p.success_criteria.length) {
        $("criteria").replaceChildren(...p.success_criteria.map((c) => h("li", null, c)));
        $("criteria-box").classList.remove("hidden");
      }
      renderPlan();
      const done = v.plan.filter((i) => i.status === "done").length;
      noteRow("plan", "plan", `Plan updated: ${done}/${v.plan.length} items done`);
      break;
    }
    case "run_queued":
      $("empty")?.remove();
      setStatus("queued");
      append(h("div", { class: "task-card" }, h("div", { class: "label" }, "Task"), h("p", null, p.task || ""),
        h("div", { class: "muted small" }, `queued · run ${S.runId.slice(0, 8)}`)));
      break;
    case "run_requeued":
      setStatus("queued");
      noteRow("guard", "resumed", p.reason === "worker_lost"
        ? "The worker running this run was lost. The run is back in the queue and another worker will continue it from its last checkpoint."
        : `The run went back to the queue (${p.reason || "handed over"}); another worker will continue it.`);
      break;
    case "run_resumed": noteRow("guard", "resumed", p.message || "Run resumed after an interruption."); break;
    case "tool_started": handleToolStarted(p); break;
    case "tool_finished": handleToolFinished(p); break;
    case "approval_requested": handleApprovalRequested(p); break;
    case "approval_resolved": handleApprovalResolved(p); break;
    case "question_asked": handleQuestionAsked(p); break;
    case "question_answered": handleQuestionAnswered(p); break;
    case "guard_triggered":
      noteRow("guard", "guard", `Guard "${p.guard}" (${p.level}): ${p.message}`);
      break;
    case "verification_started": handleVerificationStarted(p); break;
    case "verification_finished": handleVerificationFinished(p); break;
    case "memory_updated":
      v.facts.set(p.key, { key: p.key, value: String(p.value), source: p.source, durable: !!p.durable });
      renderMemory(p.key);
      break;
    case "memory_recalled":
      noteRow("memory", "plan", `Organization memory: ${(p.keys || []).length} note(s) from past verified runs added to the context (${(p.keys || []).join(", ")}).`);
      break;
    case "memory_saved":
      noteRow("memory", "verify", `Saved to organization memory (the run was verified): ${(p.keys || []).join(", ")}${(p.updated || []).length ? ` (updated: ${p.updated.join(", ")})` : ""}.`);
      break;
    case "memory_rejected":
      noteRow("memory", "guard", `Not saved to organization memory: ${(p.rejected || []).map((r) => `${r.key} (${r.detail || r.reason})`).join("; ")}.`);
      break;
    case "usage":
      v.waitRow = null; // a call went through again: the next outage gets a fresh note
      Object.assign(v.stats, { llm_calls: p.llm_calls, input_tokens: p.input_tokens, output_tokens: p.output_tokens, llm_retries: p.llm_retries || 0 });
      renderStats();
      if (p.model) showModel(p.model);
      break;
    case "model_switched":
      noteRow("guard", "model", `Switched model: ${p.from} -> ${p.to} (the previous one is out of free-tier quota or unavailable)`);
      break;
    case "llm_waiting": {
      const text = `${p.message || "Network unavailable, retrying the language model…"} (waited ${Math.round(p.waited_s || 0)}s)`;
      if (v.waitRow && v.waitRow.isConnected) v.waitRow.querySelector("span").textContent = text;
      else { v.waitRow = noteRow("guard", "guard", text); v.waitRow.classList.add("waiting"); }
      break;
    }
    case "run_finished": {
      handleRunFinished(p);
      const secs = Math.max(0, Math.round(((Date.parse(ev.ts) || Date.now()) - (v.startedAt || Date.now())) / 1000));
      $("timeline-meta").textContent = `finished in ${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(secs % 60).padStart(2, "0")}`;
      break;
    }
    case "replay_done":
      v.replaying = false;
      renderLive();
      if (!p.live) { S.finished = true; }
      break;
    case "error":
      toast(p.message || "Error", "error");
      S.finished = true;
      break;
    default: break;
  }
}

/* ------------------------------------------------------------------ websocket */

function connect(runId) {
  if (S.ws) { S.ws.onclose = null; S.ws.onmessage = null; try { S.ws.close(); } catch (e) { /* ignore */ } }
  const token = ++S.connToken;
  S.runId = runId;
  S.finished = false;
  loadRunMeta(runId);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/runs/${encodeURIComponent(runId)}/events`);
  S.ws = ws;
  ws.onopen = () => { if (token === S.connToken) { resetView(); S.view.replaying = true; } };
  ws.onmessage = (msg) => {
    if (token !== S.connToken) return;
    let ev;
    try { ev = JSON.parse(msg.data); } catch (e) { return; }
    try { handleEvent(ev); } catch (err) { console.error("event render failed", ev, err); }
  };
  ws.onclose = async () => {
    if (token !== S.connToken || S.finished) return;
    if (S.mode === "db" && !(await sessionAlive())) return; // the sign-in screen is shown instead
    setTimeout(() => { if (token === S.connToken && !S.finished) connect(runId); }, 1000);
  };
  highlightHistory();
}

/* ------------------------------------------------------------------ left column */

function selectedChaos() {
  return [...document.querySelectorAll("#chaos input[type=checkbox]:checked")].map((c) => c.value);
}

function setChaos(names) {
  const set = new Set(names || []);
  document.querySelectorAll("#chaos input[type=checkbox]").forEach((c) => { c.checked = set.has(c.value); });
}

async function loadScenarios() {
  try {
    const list = await api("GET", "/api/scenarios");
    $("presets").replaceChildren(...list.map((s) => h("button", {
      class: "chip", type: "button", title: s.task,
      onclick: () => {
        $("task").value = s.task;
        if (!canAdminEnv()) return; // only admins can change the environment's chaos flags
        setChaos(s.chaos);
        if (s.chaos && s.chaos.length) toast(`Chaos flags set: ${s.chaos.join(", ")}`, "ok");
      },
    }, s.title, s.chaos && s.chaos.length ? h("span", { class: "flag", title: `chaos: ${s.chaos.join(", ")}` }, "⚡") : null)));
  } catch (err) {
    $("presets").replaceChildren(h("span", { class: "muted small" }, "Could not load examples."));
  }
}

let chaosLoaded = false;
const canAdminEnv = () => S.mode !== "db" || !!(S.me && S.me.capabilities.admin);

async function loadChaos() {
  try {
    const data = await api("GET", "/api/sim/chaos");
    const keep = new Set(selectedChaos());
    const flags = data.flags || {};
    const first = !chaosLoaded;
    $("chaos").replaceChildren(...Object.keys(flags).map((name) => h("label", { class: "chaos-item" },
      h("input", { type: "checkbox", value: name, checked: first ? flags[name] : keep.has(name) }),
      h("span", null, h("span", { class: "chaos-name" }, name), h("span", { class: "chaos-desc" }, (data.descriptions || {})[name] || "")))));
    chaosLoaded = true;
    if (!canAdminEnv()) document.querySelectorAll("#chaos input").forEach((c) => (c.disabled = true));
  } catch (err) {
    if (!chaosLoaded) {
      $("chaos").replaceChildren(h("span", { class: "muted small" },
        S.mode === "db" && /environment/i.test(err.message) ? err.message : "Environment not reachable. Start it with `python -m company_sim`."));
    }
  }
}

async function resetEnvironment(quiet) {
  const res = await api("POST", S.mode === "db" ? "/api/admin/environment/reset" : "/api/sim/reset", { chaos: selectedChaos() });
  if (!quiet) toast(`Environment reset${res.chaos && res.chaos.length ? ` (chaos: ${res.chaos.join(", ")})` : ""}`, "ok");
  return res;
}

function updateRunButtons() {
  const db = S.mode === "db";
  // v1 runs one task at a time. In database mode runs queue up, so starting is always possible and Cancel
  // applies to the run on screen.
  const viewedLive = !!S.runId && !!S.view && LIVE_STATUSES.includes(S.view.status);
  $("run-btn").disabled = db ? false : !!S.activeRunId;
  $("cancel-btn").disabled = db ? !viewedLive : !S.activeRunId;
  $("reset-btn").disabled = !!S.activeRunId; // the environment cannot be reset while any run of the org is live
  if (db && S.me) {
    const mayRun = S.me.capabilities.start_runs;
    $("run-btn").classList.toggle("hidden", !mayRun);
    $("cancel-btn").classList.toggle("hidden", !mayRun);
  }
}

async function startRun() {
  const task = $("task").value.trim();
  if (!task) { toast("Describe a task first, or pick an example.", "error"); $("task").focus(); return; }
  if (S.config && S.config.runs_left_today === 0) {
    toast(S.mode === "db"
      ? `Your organization reached its daily limit (${S.config.max_runs_per_day} runs per day, UTC). An admin can raise it in Admin > Organization settings.`
      : `Daily demo limit reached (${S.config.max_runs_per_day} runs per day, UTC). Please try again tomorrow.`, "error");
    return;
  }
  $("run-btn").disabled = true;
  try {
    if ($("reset-before").checked && canAdminEnv()) await resetEnvironment(true);
    const res = await api("POST", "/api/runs", {
      task, approval_mode: $("approval-mode").value, headed: $("headed").checked,
    });
    S.activeRunId = res.run_id;
    S.runMeta = { id: res.run_id, started_by: res.started_by || null };
    updateRunButtons();
    S.view = S.view || freshView();
    connect(res.run_id);
    refreshHistory();
  } catch (err) {
    toast(err.message, "error");
    updateRunButtons();
  }
}

async function cancelRun() {
  const target = S.mode === "db" ? S.runId : S.activeRunId;
  if (!target) return;
  try {
    await api("POST", `/api/runs/${encodeURIComponent(target)}/cancel`);
    toast("Cancelling... the agent stops at the next safe point.");
  } catch (err) { toast(err.message, "error"); }
}

/* ------------------------------------------------------------------ history */

function relTime(iso) {
  const t = Date.parse(iso);
  if (!t) return "";
  const secs = Math.floor((Date.now() - t) / 1000);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} h ago`;
  return new Date(t).toLocaleDateString();
}

async function refreshHistory() {
  if (S.mode === "db" && !S.signedIn) return;
  try {
    const runs = await api("GET", "/api/runs");
    runs.forEach((r) => S.runIndex.set(r.id, r));
    const list = $("history");
    if (!runs.length) { list.replaceChildren(h("li", { class: "muted small" }, "No runs yet.")); return; }
    list.replaceChildren(...runs.slice(0, 40).map((r) => h("li", null,
      h("button", { class: `history-item ${r.id === S.runId ? "selected" : ""}`, type: "button", "data-run": r.id, onclick: () => loadRun(r.id) },
        h("span", { class: `dot dot-h-${r.status}` }),
        h("span", null, h("span", { class: "history-task" }, r.task || "(no task)"),
          h("span", { class: "history-meta" }, `${STATUS_LABEL[r.status] || r.status}${r.queue_position ? ` #${r.queue_position} in queue` : ""} · ${relTime(r.queued_at || r.started_at)}`
            + (r.started_by ? ` · by ${r.started_by.name || r.started_by.email}` : "")
            + (r.worker ? ` · on ${r.worker}` : "") + (r.resumed ? " · resumed" : "")))))));
    const shown = runs.find((r) => r.id === S.runId);
    if (shown && shown.status === "queued") {
      $("timeline-meta").textContent = `waiting in the queue · position ${shown.queue_position || "?"}`;
    }
  } catch (err) { /* server may be restarting */ }
}

function highlightHistory() {
  document.querySelectorAll(".history-item").forEach((b) => b.classList.toggle("selected", b.dataset.run === S.runId));
}

function loadRun(id) {
  S.view = S.view || freshView();
  connect(id);
}

/* ------------------------------------------------------------------ health */

// Deployment flags arrive with /api/health (deploy_mode, sim_proxy, auth_enabled, runs_left_today, ...).
function applyConfig(cfg) {
  S.config = cfg;
  $("headed-switch").classList.toggle("hidden", !!cfg.deploy_mode);
  if (cfg.deploy_mode) $("headed").checked = false;
  $("company-link").classList.toggle("hidden", !cfg.sim_proxy || (S.mode === "db" && !(S.me && S.me.capabilities.start_runs)));
  if (S.mode === "db") $("company-link").setAttribute("href", "/api/environment/enter");
  $("logout-link").classList.toggle("hidden", !cfg.auth_enabled || S.mode === "db");
}

async function pollHealth() {
  if (S.mode === "db" && !S.signedIn) return;
  try {
    const hl = await api("GET", "/api/health");
    $("model-badge").dataset.provider = hl.provider;
    showModel(hl.model, hl.models_unavailable);
    if (!hl.api_key_set) $("model-badge").title = "No API key configured: runs will fail to start.";
    $("sim-dot").className = `dot ${hl.sim_reachable ? "dot-ok" : "dot-bad"}`;
    $("sim-label").textContent = hl.sim_reachable ? "environment online" : "environment offline";
    if (hl.sim_reachable && !chaosLoaded) loadChaos();
    $("approval-mode").dataset.default = hl.approval_mode;
    applyConfig(hl);
    if (!pollHealth.seeded) { $("approval-mode").value = hl.approval_mode; $("headed").checked = !hl.headless; pollHealth.seeded = true; }
    const before = S.activeRunId;
    S.activeRunId = hl.active_run;
    if (hl.active_run && !S.runId) connect(hl.active_run);
    if (before !== S.activeRunId) updateRunButtons();
  } catch (err) {
    $("sim-dot").className = "dot dot-unknown";
    $("sim-label").textContent = "worker offline";
  }
}

/* ------------------------------------------------------------------ lightbox, scrolling */

function openLightbox(url, caption) {
  $("lightbox-img").src = url;
  $("lightbox-caption").textContent = caption || "";
  $("lightbox").classList.remove("hidden");
}
function closeLightbox() { $("lightbox").classList.add("hidden"); $("lightbox-img").removeAttribute("src"); }

function initScrolling() {
  const sc = $("scroller");
  sc.addEventListener("scroll", () => {
    const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 90;
    S.autoscroll = nearBottom;
    $("autoscroll-btn").classList.toggle("hidden", nearBottom);
  });
  $("autoscroll-btn").addEventListener("click", () => {
    S.autoscroll = true;
    $("autoscroll-btn").classList.add("hidden");
    sc.scrollTop = sc.scrollHeight;
  });
}

/* ------------------------------------------------------------------ multi-user mode: sign-in, header, roles */

// File mode (v1) answers /api/auth/me with 404 or {mode: "file"}; database mode with the profile or 401.
async function detectMode() {
  try {
    const resp = await fetch("/api/auth/me");
    if (resp.status === 404) return { mode: "file" };
    const data = await resp.json().catch(() => ({}));
    if (resp.ok && data.mode === "db") return { mode: "db", me: data };
    if (resp.status === 401 && data.detail === "Not signed in") return { mode: "db", me: null };
  } catch (err) { /* offline: behave like v1 and let the health poll show it */ }
  return { mode: "file" };
}

async function sessionAlive() {
  try {
    const resp = await fetch("/api/auth/me");
    if (resp.ok) return true;
    if (resp.status === 401) showLogin("Your session has ended. Please sign in again.");
  } catch (err) { return true; /* network trouble: keep trying */ }
  return false;
}

function showLogin(message) {
  S.signedIn = false;
  document.body.classList.add("signed-out");
  if (S.ws) { S.ws.onclose = null; S.ws.onmessage = null; try { S.ws.close(); } catch (e) { /* ignore */ } S.ws = null; }
  if (window.stopApprovals) window.stopApprovals();
  $("login-screen").classList.remove("hidden");
  $("user-box").classList.add("hidden");
  $("main-nav").classList.add("hidden");
  setLoginError(message || "");
  $("login-password").value = "";
  setTimeout(() => ($("login-email").value ? $("login-password") : $("login-email")).focus(), 30);
}

function setLoginError(message) {
  const box = $("login-error");
  box.textContent = message;
  box.classList.toggle("hidden", !message);
}

async function submitLogin(ev) {
  ev.preventDefault();
  const button = $("login-submit");
  button.disabled = true;
  setLoginError("");
  try {
    const resp = await fetch("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("login-email").value.trim(), password: $("login-password").value }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      setLoginError(typeof data.detail === "string" ? data.detail : "Sign-in failed.");
      $("login-password").value = "";
      $("login-password").focus();
      return;
    }
    $("login-password").value = "";
    enterApp(data);
  } catch (err) {
    setLoginError("Could not reach the server. Check your connection and try again.");
  } finally {
    button.disabled = false;
  }
}

function enterApp(me) {
  S.me = me;
  S.signedIn = true;
  document.body.classList.remove("signed-out");
  $("login-screen").classList.add("hidden");
  S.runId = null;
  S.runMeta = null;
  chaosLoaded = false;
  showView("dashboard");
  resetView();
  applyMe();
  startApp();
}

function applyMe() {
  const me = S.me, caps = me.capabilities, org = me.org;
  $("user-box").classList.remove("hidden");
  $("user-name").textContent = me.user.name;
  $("role-badge").textContent = org.role;
  $("role-badge").className = `role-badge role-${org.role}`;
  const sw = $("org-switch");
  sw.classList.toggle("hidden", me.orgs.length < 2);
  $("org-name").classList.toggle("hidden", me.orgs.length > 1);
  $("org-name").textContent = org.name;
  sw.replaceChildren(...me.orgs.map((o) => h("option", { value: o.id, selected: o.id === org.id }, `${o.name} (${o.role})`)));
  $("main-nav").classList.remove("hidden");
  $("nav-approvals").classList.remove("hidden");
  $("nav-memory").classList.remove("hidden");
  document.querySelectorAll(".nav-btn[data-view=admin], .nav-btn[data-view=credentials]").forEach((b) => b.classList.toggle("hidden", !caps.admin));
  $("logout-link").classList.add("hidden");
  $("task").readOnly = !caps.start_runs;
  $("headed-switch").classList.add("hidden");
  $("reset-btn").classList.toggle("hidden", !caps.admin);
  $("reset-before").closest("label").classList.toggle("hidden", !caps.admin);
  const note = $("access-note");
  let text = "";
  if (!caps.start_runs) text = `You have read-only access (${org.role}). You can watch runs${caps.approve ? " and answer approvals" : ""} but not start them.`;
  else if (!org.environment_ready) text = "This organization has no simulated company yet. An admin must provision it under Admin > Environment before runs can start.";
  note.textContent = text;
  note.classList.toggle("hidden", !text);
  updateRunButtons();
}

async function switchOrg(orgId) {
  try {
    await api("POST", "/api/auth/switch-org", { org_id: orgId });
    location.reload(); // the simplest way to drop every bit of state of the previous organization
  } catch (err) {
    toast(err.message, "error");
    applyMe();
  }
}

async function signOut() {
  try { await fetch("/api/auth/logout", { method: "POST" }); } catch (err) { /* cleared server-side when reachable */ }
  S.me = null;
  S.runId = null;
  S.runMeta = null;
  S.runIndex.clear();
  resetView();
  $("history").replaceChildren(h("li", { class: "muted small" }, "No runs yet."));
  showLogin("");
}

// Which run is on screen, and who started it (the approval card needs the starter for separation of duties).
async function loadRunMeta(runId) {
  S.runMeta = S.runIndex.get(runId) || (S.runMeta && S.runMeta.id === runId ? S.runMeta : null);
  if (S.mode !== "db") return;
  if (!S.runMeta || !S.runMeta.started_by) {
    try {
      const data = await api("GET", `/api/runs/${encodeURIComponent(runId)}`);
      if (S.runId === runId) S.runMeta = { id: runId, started_by: data.started_by };
    } catch (err) { return; }
  }
  if (S.view && S.view.gate) S.view.gate();
}

function showView(name) {
  const views = { dashboard: "view-dashboard", approvals: "view-approvals", memory: "view-memory", admin: "view-admin", credentials: "view-credentials" };
  Object.entries(views).forEach(([key, id]) => $(id).classList.toggle("hidden", key !== name));
  document.querySelectorAll(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  if (name === "approvals" && window.loadApprovalsPage) window.loadApprovalsPage();
  if (name === "memory" && window.loadMemoryPage) window.loadMemoryPage();
  if (name === "admin" && window.loadAdminPage) window.loadAdminPage();
  if (name === "credentials" && window.loadCredentialsPage) window.loadCredentialsPage();
}

/* ------------------------------------------------------------------ boot */

function startApp() {
  if (S.mode === "db" && window.startApprovals) window.startApprovals();
  loadScenarios();
  refreshHistory();
  pollHealth();
  if (S.started) return; // the timers below run once, across sign-outs and sign-ins
  S.started = true;
  setInterval(pollHealth, 4000);
  setInterval(tickTimer, 1000);
  setInterval(refreshHistory, S.mode === "db" ? 5000 : 15000);
}

function init() {
  resetView();
  initScrolling();
  $("run-btn").addEventListener("click", startRun);
  $("cancel-btn").addEventListener("click", cancelRun);
  $("reset-btn").addEventListener("click", async () => {
    $("reset-btn").disabled = true;
    try { await resetEnvironment(false); await loadChaos(); } catch (err) { toast(err.message, "error"); }
    updateRunButtons();
  });
  $("history-refresh").addEventListener("click", refreshHistory);
  $("live-btn").addEventListener("click", () => { const l = S.view && S.view.live; if (l) openLightbox(l.url, l.caption); });
  $("lightbox").addEventListener("click", (e) => { if (e.target.id !== "lightbox-img") closeLightbox(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeLightbox(); });
  $("task").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) startRun(); });
  $("login-form").addEventListener("submit", submitLogin);
  $("logout-btn").addEventListener("click", signOut);
  $("org-switch").addEventListener("change", (e) => switchOrg(e.target.value));
  document.querySelectorAll(".nav-btn").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
  updateRunButtons();
}

async function boot() {
  init();
  const detected = await detectMode();
  S.mode = detected.mode;
  if (S.mode === "file") { startApp(); return; }
  if (detected.me) enterApp(detected.me); else showLogin("");
}

document.addEventListener("DOMContentLoaded", boot);
