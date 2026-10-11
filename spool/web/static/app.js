// Spool web: renders a session's event log as it streams in (Server-Sent Events).
// The server replays everything after Last-Event-ID, so reconnects never lose or repeat events.

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const S = {
  readOnly: false,
  connectors: [],
  view: "sessions",
  editing: null,
  current: null,
  stream: null,
  lastSeq: -1,
  calls: new Map(),
  step: 0,
  summary: null,
  filesPath: ".",
  live: false,
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

// ------------------------------------------------------------------ labels
const FINAL_LABELS = {
  completed: "Done",
  failed: "Failed",
  max_steps: "Stopped at the step limit",
  budget_exhausted: "Stopped at the token budget",
  cancelled: "Stopped",
};

function describeSession(s) {
  if (s.running) return s.awaiting ? { text: "Waiting for your approval", cls: "waiting" } : { text: "Working", cls: "" };
  const status = s.status;
  if (status === "awaiting_approval") return { text: "Waiting for your approval", cls: "waiting" };
  if (status === "sleeping") return { text: "Sleeping until it is resumed", cls: "" };
  if (status === "finished") {
    const f = s.final && s.final.status;
    return { text: FINAL_LABELS[f] || "Finished", cls: f === "completed" ? "" : "failed" };
  }
  if (status in FINAL_LABELS) return { text: FINAL_LABELS[status], cls: status === "completed" ? "" : "failed" };
  return { text: "Interrupted. Resume continues from the last recorded step.", cls: "waiting" };
}

const RISK = [
  ["untrusted_input", "Read untrusted content"],
  ["sensitive_data", "Read protected data"],
  ["external_comm", "Sent data out"],
];

const FIRST_ARGS = ["path", "command", "url", "to", "task", "call_id"];

function shortArgs(args) {
  if (!args) return "";
  const rank = (k) => (FIRST_ARGS.includes(k) ? FIRST_ARGS.indexOf(k) : FIRST_ARGS.length);
  const entries = Object.entries(args).sort(([a], [b]) => rank(a) - rank(b));
  const parts = entries.map(([k, v]) => {
    const text = typeof v === "string" ? v : JSON.stringify(v);
    return `${k}=${text.replace(/\s+/g, " ")}`;
  });
  return parts.join("  ");
}

function lineCount(text) {
  const n = text ? text.split("\n").length : 0;
  return `${n} line${n === 1 ? "" : "s"}`;
}

// The interface is written in English, so dates are too (in the browser's time zone).
function when(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function clockTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

// Why a step needs approval, for a person (the policy's own reason is written for the model).
function approvalWhy(rule, reason) {
  if (rule === "rule_of_two") {
    return "Together with what this run has already done, this step would read untrusted content, read protected "
      + "data and send data out. That combination can leak data if the agent was misled by something it read.";
  }
  if (rule === "confirm") return "This step sends, buys or changes something outside the agent's folder.";
  if (rule === "user_rule") return "One of your standing rules asks you to approve steps like this.";
  return reason || "The policy asks for a person to decide on this step.";
}

const RUN_LABELS = {
  running: { text: "running", cls: "" }, awaiting_approval: { text: "waiting for approval", cls: "waiting" },
  sleeping: { text: "sleeping", cls: "" }, completed: { text: "done", cls: "" }, failed: { text: "failed", cls: "failed" },
  max_steps: { text: "stopped at the step limit", cls: "failed" }, budget_exhausted: { text: "stopped at the budget", cls: "failed" },
  "not started": { text: "starting", cls: "" },
};

// ------------------------------------------------------------------ views
const PAGES = { routines: "page-routines", approvals: "page-approvals", memory: "page-memory" };

function showView(view) {
  S.view = view;
  const sessions = view === "sessions";
  document.querySelector(".app").classList.toggle("wide", !sessions);  // no inspector outside sessions
  for (const [name, id] of Object.entries(PAGES)) $(id).hidden = name !== view;
  $("composer").hidden = !sessions || S.readOnly;
  $("entries").hidden = !sessions;
  if (!sessions) {
    $("intro").hidden = true;
    $("ledger-head").hidden = true;
    $("panel-session").hidden = true;
    $("panel-files").hidden = true;
    if (S.stream) S.stream.close();
    S.current = null;
  }
  $("nav-sessions").setAttribute("aria-current", sessions ? "page" : "false");
  $("nav-routines").setAttribute("aria-current", view === "routines" ? "page" : "false");
  $("nav-approvals").setAttribute("aria-current", view === "approvals" ? "page" : "false");
  $("nav-memory").setAttribute("aria-current", view === "memory" ? "page" : "false");
}

function route() {
  const hash = location.hash;
  const match = hash.match(/^#\/s\/(.+)$/);
  if (match) {
    if (!(S.view === "sessions" && S.current === match[1])) openSession(match[1]);
  } else if (hash === "#/routines") {
    showView("routines");
    loadRoutines();
    loadSessions();
  } else if (hash === "#/approvals") {
    showView("approvals");
    loadApprovals();
    loadSessions();
  } else if (hash === "#/memory") {
    showView("memory");
    loadMemory();
    loadSessions();
  } else if (S.view !== "sessions" || S.current) {
    showNewTask();
  }
}

async function refreshApprovalCount() {
  try {
    const items = await api("/api/approvals");
    const badge = $("approval-count");
    const changed = badge.textContent !== String(items.length);
    badge.hidden = items.length === 0;
    badge.textContent = String(items.length);
    if (S.view === "approvals") renderApprovals(items);
    if (changed) loadSessions();  // the rail shows which sessions are waiting
  } catch (_) { /* next poll */ }
}

// ------------------------------------------------------------------- rail
async function loadSessions() {
  const rows = await api("/api/sessions");
  const list = $("session-list");
  list.replaceChildren();
  for (const row of rows) {
    const label = describeSession({ ...row, status: row.status, running: row.running });
    const button = el("button", {
      class: "session-item", type: "button", "aria-current": row.id === S.current ? "true" : "false",
      onclick: () => openSession(row.id),
    }, el("span", { class: "t", text: row.task }), el("span", { class: `s ${label.cls}`, text: label.text }));
    list.append(el("li", {}, button));
  }
  if (!rows.length) list.append(el("li", { class: "session-item" }, el("span", { class: "s", text: "No sessions yet." })));
  return rows;
}

// --------------------------------------------------------------- composer
function setComposerMode() {
  const isNew = !S.current;
  $("composer-options").hidden = !isNew;
  const running = S.summary && S.summary.running;
  $("submit").textContent = isNew ? "Start task" : "Send";
  $("submit").disabled = !isNew && running;
  $("stop").hidden = isNew || !running;
  $("composer-text").placeholder = isNew
    ? "Describe a task, e.g. The tests in test_stats.py fail. Fix stats.py so they pass."
    : "Send a follow-up, e.g. Now add a test for empty input.";
  const hint = $("composer-hint");
  hint.classList.remove("red");
  if (!isNew && running) hint.textContent = "The agent is working. You can leave; the run continues on the server.";
  else if (!isNew && S.summary && S.summary.error) {
    hint.textContent = S.summary.error;
    hint.classList.add("red");
  } else hint.textContent = "";
}

async function submitComposer(event) {
  event.preventDefault();
  const text = $("composer-text").value.trim();
  if (!text) {
    $("composer-hint").textContent = S.current ? "Write a message first." : "Describe the task first.";
    return;
  }
  try {
    if (!S.current) {
      const created = await api("/api/sessions", {
        method: "POST",
        body: {
          task: text,
          approval: $("opt-approval").value,
          context_strategy: $("opt-context").value,
          workspace: $("opt-workspace").value.trim() || undefined,
          connectors: checkedServices("opt-services"),
          memory: $("opt-memory").checked,
        },
      });
      $("composer-text").value = "";
      await loadSessions();
      openSession(created.id);
    } else {
      await api(`/api/sessions/${S.current}/messages`, { method: "POST", body: { text } });
      $("composer-text").value = "";
    }
  } catch (err) {
    $("composer-hint").textContent = err.message;
    $("composer-hint").classList.add("red");
  }
}

function showNewTask() {
  showView("sessions");
  if (S.readOnly) {
    $("intro").hidden = false;
    $("ledger-head").hidden = true;
    $("entries").replaceChildren();
    return;
  }
  if (S.stream) S.stream.close();
  S.current = null;
  S.summary = null;
  if (location.hash && location.hash !== "#/") history.replaceState(null, "", "#/");
  $("ledger-head").hidden = true;
  $("intro").hidden = false;
  $("entries").replaceChildren();
  $("panel-session").hidden = true;
  $("panel-files").hidden = true;
  setComposerMode();
  loadSessions();
  $("composer-text").focus();
}

// ----------------------------------------------------------------- session
function openSession(id) {
  showView("sessions");
  if (S.stream) S.stream.close();
  Object.assign(S, { current: id, lastSeq: -1, step: 0, summary: null, filesPath: ".", live: false });
  S.calls.clear();
  if (location.hash !== `#/s/${id}`) history.pushState(null, "", `#/s/${id}`);
  $("intro").hidden = true;
  $("entries").replaceChildren();
  $("ledger-head").hidden = false;
  $("panel-session").hidden = false;
  $("panel-files").hidden = false;
  $("file-view").hidden = true;
  setComposerMode();
  loadSessions();
  loadFiles(".");

  const stream = new EventSource(`/api/sessions/${id}/events`);
  stream.addEventListener("event", (e) => onEvent(JSON.parse(e.data)));
  stream.addEventListener("status", (e) => {
    S.live = true;  // everything recorded so far has been replayed
    onStatus(JSON.parse(e.data));
  });
  S.stream = stream;
}

const refreshSummary = debounce(async () => {
  if (!S.current) return;
  try {
    onStatus(await api(`/api/sessions/${S.current}`));
  } catch (_) { /* the stream will catch up */ }
}, 250);

const refreshFiles = debounce(() => S.current && loadFiles(S.filesPath, true), 400);

function onStatus(summary) {
  if (summary.id !== S.current) return;
  S.summary = summary;
  setComposerMode();
  renderInspector(summary);
  loadSessions();
}

// ------------------------------------------------------------------ events
function nearBottom() {
  const box = $("entries");
  return box.scrollHeight - box.scrollTop - box.clientHeight < 120;
}

function addEntry(seq, body, extraClass = "") {
  const stick = nearBottom();
  const entry = el("li", { class: `entry ${extraClass} ${S.live ? "fresh" : ""}`, "data-seq": seq },
    el("div", { class: "seq", text: String(seq) }), body);
  $("entries").append(entry);
  if (stick) $("entries").scrollTop = $("entries").scrollHeight;
  return entry;
}

function onEvent(ev) {
  if (ev.seq <= S.lastSeq) return;
  S.lastSeq = ev.seq;
  const handler = HANDLERS[ev.type];
  if (handler) handler(ev, ev.data);
  if (S.live && (ev.type === "model_response" || ev.type === "tool_completed" || ev.type === "session_finished")) {
    refreshSummary();
  }
  if (S.live && ev.type === "tool_completed") refreshFiles();
}

function fact(label, value) {
  return el("div", {}, el("dt", { text: label }), el("dd", { text: value }));
}

const CONTEXT_LABELS = { offload: "files when needed", full: "full history", fold: "fold old output", summarize: "summarize" };
const APPROVAL_LABELS = { human: "ask me", auto_deny: "block", auto_approve: "allow" };

const HANDLERS = {
  session_started(ev, d) {
    const cfg = d.config || {};
    $("task-title").textContent = d.task;
    const facts = $("run-facts");
    facts.replaceChildren(
      fact("Model", cfg.model),
      fact("Sandbox", cfg.sandbox),
      fact("Approvals", (APPROVAL_LABELS[cfg.approval] || cfg.approval)
        + (cfg.approval === "human" && cfg.approval_ttl_s ? `, skipped after ${Math.round(cfg.approval_ttl_s / 60)} min` : "")),
      fact("Context", CONTEXT_LABELS[cfg.context_strategy] || cfg.context_strategy),
      fact("Folder", d.workspace),
    );
    if (d.parent && d.parent.kind === "fork") {
      facts.append(fact("Branched from", `${d.parent.session_id} at event ${d.parent.seq}`));
    }
    if (d.parent && d.parent.kind === "routine") facts.append(fact("Started by", `routine ${d.parent.routine}`));
    if ((cfg.connectors || []).length) facts.append(fact("Services", cfg.connectors.join(", ")));
    if (cfg.memory) facts.append(fact("Memory", "on"));
    if ((cfg.skills || []).length) facts.append(fact("Skills", cfg.skills.includes("*") ? "all installed" : cfg.skills.join(", ")));
  },

  user_message(ev, d) {
    addEntry(ev.seq, el("div", { class: "body user" }, el("div", { class: "who", text: "You" }),
      el("div", { class: "prose", text: d.text })));
  },

  model_response(ev, d) {
    S.step += 1;
    const msg = d.message || {};
    const forkButton = S.readOnly ? null : el("button", {
      class: "btn-link branch", type: "button", text: "Branch from here",
      title: "Start a new session with the history up to this step and let the agent try again",
      onclick: () => branchFrom(ev.seq - 1),
    });
    const usage = d.usage || {};
    const meta = `${usage.input_tokens || 0} prompt tokens, ${usage.cached_tokens || 0} from cache, ${((d.latency_ms || 0) / 1000).toFixed(1)} s`;
    const body = el("div", { class: "body" },
      el("div", { class: "who" }, el("span", { text: `Step ${S.step}` }), el("span", { text: meta }), forkButton));
    if (msg.content) body.append(el("div", { class: "prose", text: msg.content }));
    S.lastContent = msg.content || "";
    for (const call of msg.tool_calls || []) {
      const state = el("span", { class: "call-state", text: call.parse_error ? "invalid call" : "queued" });
      const result = el("div", { class: "call-result" });
      const notes = el("div", { class: "call-notes" });
      const args = call.arguments && Object.keys(call.arguments).length ? call.arguments : call.raw_arguments;
      const root = el("div", { class: "call" },
        el("div", { class: "call-head" },
          el("span", { class: "call-name", text: call.name }),
          el("span", { class: "call-args", text: typeof args === "string" ? args : shortArgs(args) }),
          state),
        el("details", {}, el("summary", { text: "Arguments" }),
          el("pre", { text: typeof args === "string" ? args : JSON.stringify(args, null, 2) })),
        notes, result);
      S.calls.set(call.id, { root, state, result, notes, name: call.name });
      body.append(root);
    }
    addEntry(ev.seq, body, "step-start");
  },

  model_error(ev, d) {
    addEntry(ev.seq, el("div", { class: "body" }, el("div", { class: "error-line", text: `Model call failed: ${d.error}` })));
  },

  policy_decision(ev, d) {
    const call = S.calls.get(d.call_id);
    if (!call || (d.verdict === "allow" && d.rule !== "user_rule")) return;
    if (d.verdict === "deny") {
      call.root.className = "call denied";
      call.state.textContent = "blocked";
      const text = d.rule === "user_rule" && d.user_rule ? `Blocked by your rule: never allow ${d.user_rule.tool}.` : d.reason;
      call.notes.append(el("p", { class: "note-line red", text }));
    } else if (d.rule === "user_rule" && d.user_rule && d.user_rule.effect === "allow") {
      call.notes.append(el("p", { class: "note-line", text: `Allowed by your rule for ${d.user_rule.tool}.` }));
    }
  },

  approval_requested(ev, d) {
    const call = S.calls.get(d.call_id);
    if (!call) return;
    call.root.className = "call awaiting";
    call.state.textContent = "needs your approval";
    const decide = (approved, always = false) => async () => {
      box.querySelectorAll("button").forEach((b) => (b.disabled = true));
      try {
        await api(`/api/sessions/${S.current}/approvals`, { method: "POST", body: { call_id: d.call_id, approved, always } });
      } catch (err) {
        box.append(el("p", { class: "note-line red", text: err.message }));
        box.querySelectorAll("button").forEach((b) => (b.disabled = false));
      }
    };
    const box = el("div", { class: "approval" },
      el("p", { text: `Allow this step? ${approvalWhy(d.rule || "rule_of_two", d.reason)}` }),
      d.expires_at ? el("p", { class: "note-line amber", text: `If nobody answers by ${clockTime(d.expires_at)}, it will not run.` }) : null,
      S.readOnly ? null : el("div", { class: "actions" },
        el("button", { class: "btn btn-primary", type: "button", text: "Allow once", onclick: decide(true) }),
        d.rule === "confirm" ? el("button", { class: "btn", type: "button", text: `Always allow ${d.tool}`,
          onclick: decide(true, true) }) : null,
        el("button", { class: "btn btn-danger", type: "button", text: "Deny", onclick: decide(false) })));
    call.approval = box;
    call.notes.append(box);
  },

  approval_decided(ev, d) {
    const call = S.calls.get(d.call_id);
    if (!call || !call.approval) return;
    call.approval.className = "approval decided";
    const who = d.by === "web" ? "You" : d.by;
    const text = d.by === "expired" ? "Nobody answered in time, so this step did not run."
      : `${who} ${d.approved ? "allowed" : "denied"} this step.`;
    call.approval.replaceChildren(el("p", { text }));
    call.expired = d.by === "expired";
    refreshApprovalCount();
    call.root.className = "call";
    call.state.textContent = d.approved ? "allowed" : "denied";
  },

  tool_started(ev, d) {
    const call = S.calls.get(d.call_id);
    if (call) call.state.textContent = "running";
  },

  tool_completed(ev, d) {
    const call = S.calls.get(d.call_id);
    if (!call) return;
    const kind = d.ok ? "ok" : (d.kind === "executed" ? "error" : d.kind);
    call.root.className = `call ${kind}`;
    const labels = { ok: "done", error: "failed", denied: "blocked", rejected: "denied", invalid: "invalid call",
      interrupted: "interrupted", deduplicated: "done (already sent)" };
    call.state.textContent = d.kind === "deduplicated" ? labels.deduplicated
      : call.expired ? "skipped, no answer" : (labels[kind] || kind);
    // Denials and rejections are explained by the policy note or the approval box; their error
    // text is the instruction written for the model.
    if (!d.ok && d.error && d.kind !== "denied" && d.kind !== "rejected") {
      call.notes.append(el("p", { class: "note-line red", text: d.error }));
    }
    const output = d.offloaded_path ? d.preview : d.output;
    if (output) {
      call.result.append(el("details", {}, el("summary", { text: `Output, ${lineCount(d.output)}` }),
        el("pre", { text: output })));
    }
    if (d.offloaded_path) {
      call.notes.append(el("p", { class: "note-line", text: `Long output saved to ${d.offloaded_path}; the model saw a preview.` }));
    }
    if (d.loop_warning) call.notes.append(el("p", { class: "note-line amber", text: "Repeated call: the agent was told to change approach." }));
  },

  context_folded(ev, d) {
    addEntry(ev.seq, el("div", { class: "body divider",
      text: `Output from steps 1 to ${d.through_step} is now shown to the model as one-line notes. It can recall any of them.` }));
  },

  context_compacted(ev, d) {
    addEntry(ev.seq, el("div", { class: "body divider", text: "Older steps were replaced by a summary to save context." }));
  },

  note(ev, d) {
    const text = d.kind === "recovery" ? `Resumed after an interruption and ran ${d.tool} again.`
      : d.kind === "woke" ? "Woke up and continued." : null;
    if (text) addEntry(ev.seq, el("div", { class: "body divider", text }));
  },

  session_finished(ev, d) {
    const ok = d.status === "completed";
    const body = el("div", { class: `body final ${ok ? "" : "bad"}` },
      el("div", { class: "verdict", text: FINAL_LABELS[d.status] || d.status }));
    // The final answer is usually the last step's text, already shown just above.
    if (d.answer && d.answer !== S.lastContent) body.append(el("div", { class: "prose", text: d.answer }));
    else if (!d.answer && d.reason) body.append(el("div", { class: "prose", text: d.reason }));
    addEntry(ev.seq, body, "step-start");
  },
};

async function branchFrom(atSeq) {
  try {
    const created = await api(`/api/sessions/${S.current}/fork`, { method: "POST", body: { at_seq: atSeq } });
    await loadSessions();
    openSession(created.id);
  } catch (err) {
    $("composer-hint").textContent = err.message;
  }
}

// ---------------------------------------------------------------- inspector
function renderInspector(s) {
  const label = describeSession(s);
  const line = $("status-line");
  line.textContent = label.text;
  line.className = `status-line ${label.cls}`;
  const u = s.usage || {};
  const reuse = u.input_tokens ? Math.round((100 * u.cached_tokens) / u.input_tokens) : 0;
  const numbers = $("numbers");
  numbers.replaceChildren(
    el("dt", { text: "Steps" }), el("dd", { text: `${s.step} of ${s.config.max_steps}` }),
    el("dt", { text: "Prompt tokens" }), el("dd", { text: (u.input_tokens || 0).toLocaleString() }),
    el("dt", { text: "From cache" }), el("dd", { text: `${reuse}%` }),
    el("dt", { text: "Output tokens" }), el("dd", { text: (u.output_tokens || 0).toLocaleString() }),
    el("dt", { text: "Model time" }), el("dd", { text: `${Math.round((u.model_latency_ms || 0) / 1000)} s` }),
  );
  const steps = s.steps || [];
  $("cache-chart").hidden = steps.length === 0;
  const peak = Math.max(1, ...steps.map((x) => x.input));
  $("bars").replaceChildren(...steps.map((x, i) => {
    const cached = Math.min(x.cached, x.input);
    return el("div", { class: "bar", title: `Step ${i + 1}: ${x.input} prompt tokens, ${cached} from cache` },
      el("span", { class: "c", style: `height:${(60 * cached) / peak}px` }),
      el("span", { class: "f", style: `height:${(60 * (x.input - cached)) / peak}px` }));
  }));
  const taint = new Set(s.taint || []);
  $("risk").replaceChildren(...RISK.map(([key, text]) => el("span", { class: `tag ${taint.has(key) ? "on" : ""}`, text })));
  const resume = $("resume");
  const interrupted = !s.running && ["running", "sleeping", "awaiting_approval"].includes(s.status) && !s.awaiting;
  resume.hidden = !interrupted || S.readOnly;
}

// -------------------------------------------------------------------- files
async function loadFiles(path, keepView = false) {
  if (!S.current) return;
  let data;
  try {
    data = await api(`/api/sessions/${S.current}/files?path=${encodeURIComponent(path)}`);
  } catch (err) {
    $("files").replaceChildren(el("li", { class: "empty", text: err.message }));
    return;
  }
  if (data.entries === undefined) {
    const view = $("file-view");
    view.hidden = false;
    view.textContent = data.binary ? `Binary file, ${data.size} bytes.` : data.content + (data.truncated ? "\n…" : "");
    return;
  }
  S.filesPath = path;
  if (!keepView) $("file-view").hidden = true;
  const crumbs = $("crumbs");
  const parts = path === "." ? [] : path.split("/");
  crumbs.replaceChildren(el("button", { type: "button", text: "folder", onclick: () => loadFiles(".") }));
  parts.forEach((part, i) => {
    crumbs.append(" / ", el("button", { type: "button", text: part, onclick: () => loadFiles(parts.slice(0, i + 1).join("/")) }));
  });
  const list = $("files");
  list.replaceChildren();
  if (!data.entries.length) list.append(el("li", { class: "empty", text: "Empty so far." }));
  for (const entry of data.entries) {
    const name = entry.dir ? `${entry.name}/` : entry.name;
    list.append(el("li", {},
      el("button", { type: "button", text: name, onclick: () => loadFiles(entry.path) }),
      entry.dir ? null : el("span", { class: "size", text: `${entry.size} B` })));
  }
}

// ----------------------------------------------------------------- routines
const CRON_DAILY = /^(\d{1,2}) (\d{1,2}) \* \* (\*|1-5)$/;

async function loadRoutines() {
  const list = $("routine-list");
  let rows;
  try {
    rows = await api("/api/routines");
  } catch (err) {
    list.replaceChildren(el("li", { class: "empty-line", text: err.message }));
    return;
  }
  list.replaceChildren();
  if (!rows.length) list.append(el("li", { class: "empty-line", text: "No routines yet. Create one below." }));
  for (const r of rows) list.append(routineItem(r));
  $("routine-form").hidden = S.readOnly;
}

function routineItem(r) {
  const spec = r.spec;
  const approval = (spec.config || {}).approval || "human";
  const approvalText = approval === "human"
    ? `Asks you; skipped after ${Math.round((spec.approval_ttl_s || 600) / 60)} min` : approval === "auto_deny" ? "Blocked" : "Allowed";
  const actions = S.readOnly ? null : el("span", { class: "actions" },
    el("button", { class: "btn", type: "button", text: "Run now", onclick: () => runRoutine(r.name) }),
    el("button", { class: "btn", type: "button", text: r.enabled ? "Pause" : "Resume",
      onclick: () => routineAction(r.name, r.enabled ? "pause" : "resume") }),
    el("button", { class: "btn", type: "button", text: "Edit", onclick: () => editRoutine(spec) }),
    deleteButton(r.name));
  const runs = el("ul", { class: "runs" }, ...r.runs.slice(0, 5).map((run) => {
    const label = RUN_LABELS[run.status] || { text: run.status, cls: "" };
    return el("li", {}, el("a", { href: `#/s/${run.session_id}`, text: run.session_id }),
      el("span", { class: `st ${label.cls}`, text: label.text }), el("span", { class: "st", text: when(run.fired_at) }));
  }));
  return el("li", { class: `routine ${r.enabled ? "" : "off"}` },
    el("div", { class: "routine-head" },
      el("span", { class: "routine-name", text: r.name }),
      el("span", { class: "routine-when", text: r.triggers.join(", ") || "Only when started" }),
      actions),
    el("p", { class: "routine-task", text: spec.task }),
    el("div", { class: "routine-meta" },
      el("span", { text: !r.enabled ? "Paused" : r.next_at ? `Next: ${when(r.next_at)}` : "No scheduled time" }),
      el("span", { text: `Approvals: ${approvalText}` }),
      ((spec.config || {}).connectors || []).length ? el("span", { text: `Services: ${spec.config.connectors.join(", ")}` }) : null,
      el("span", { text: `Folder: ${r.workspace}` })),
    r.runs.length ? runs : el("p", { class: "routine-meta", text: "Has not run yet." }));
}

function deleteButton(name) {
  const button = el("button", { class: "btn btn-danger", type: "button", text: "Delete" });
  let armed = null;
  button.addEventListener("click", async () => {
    if (!armed) {  // two clicks, no modal dialog
      button.textContent = "Click again to delete";
      armed = setTimeout(() => { button.textContent = "Delete"; armed = null; }, 3000);
      return;
    }
    clearTimeout(armed);
    await api(`/api/routines/${name}`, { method: "DELETE" }).catch((err) => ($("routine-hint").textContent = err.message));
    loadRoutines();
  });
  return button;
}

async function runRoutine(name) {
  try {
    const started = await api(`/api/routines/${name}/run`, { method: "POST", body: {} });
    if (started.session_id) location.hash = `#/s/${started.session_id}`;
  } catch (err) {
    $("routine-hint").textContent = err.message;
  }
}

async function routineAction(name, action) {
  await api(`/api/routines/${name}/${action}`, { method: "POST", body: {} }).catch((err) => ($("routine-hint").textContent = err.message));
  loadRoutines();
}

function setWhenFields() {
  const mode = $("r-when").value;
  $("r-time-wrap").hidden = !(mode === "daily" || mode === "weekdays");
  $("r-every-wrap").hidden = mode !== "interval";
  $("r-cron-wrap").hidden = mode !== "cron";
  $("r-ttl-wrap").hidden = $("r-approval").value !== "human";
}

function resetRoutineForm() {
  S.editing = null;
  $("routine-form").reset();
  $("r-name").readOnly = false;
  $("routine-form-title").textContent = "New routine";
  $("routine-cancel").hidden = true;
  $("routine-hint").textContent = "";
  setWhenFields();
}

function editRoutine(spec) {
  S.editing = spec.name;
  $("routine-form-title").textContent = `Edit ${spec.name}`;
  $("r-name").value = spec.name;
  $("r-name").readOnly = true;
  $("r-task").value = spec.task;
  $("r-workspace").value = spec.workspace || "";
  $("r-approval").value = (spec.config || {}).approval || "human";
  $("r-ttl").value = Math.round((spec.approval_ttl_s || 600) / 60);
  const time = spec.triggers.find((t) => t.kind === "cron" || t.kind === "interval");
  $("r-when").value = "none";
  if (time && time.kind === "interval") {
    $("r-when").value = "interval";
    $("r-every").value = Math.round(time.every_s / 60);
  } else if (time) {
    const m = time.cron.match(CRON_DAILY);
    if (m) {
      $("r-when").value = m[3] === "1-5" ? "weekdays" : "daily";
      $("r-time").value = `${m[2].padStart(2, "0")}:${m[1].padStart(2, "0")}`;
    } else {
      $("r-when").value = "cron";
      $("r-cron").value = time.cron;
    }
  }
  const watch = spec.triggers.find((t) => t.kind === "file");
  $("r-watch").value = watch ? watch.path : "";
  $("r-webhook").checked = spec.triggers.some((t) => t.kind === "webhook");
  setServices("r-services", (spec.config || {}).connectors || []);
  $("r-memory").checked = Boolean((spec.config || {}).memory);
  $("routine-cancel").hidden = false;
  setWhenFields();
  $("r-task").focus();
}

async function saveRoutine(event) {
  event.preventDefault();
  const triggers = [];
  const mode = $("r-when").value;
  const [h, m] = ($("r-time").value || "09:00").split(":").map(Number);
  if (mode === "daily") triggers.push({ kind: "cron", cron: `${m} ${h} * * *` });
  if (mode === "weekdays") triggers.push({ kind: "cron", cron: `${m} ${h} * * 1-5` });
  if (mode === "interval") triggers.push({ kind: "interval", every_s: Math.max(1, Number($("r-every").value)) * 60 });
  if (mode === "cron") triggers.push({ kind: "cron", cron: $("r-cron").value.trim() });
  const watch = $("r-watch").value.trim().replace(/\/+$/, "");
  if (watch) triggers.push({ kind: "file", path: watch });
  if ($("r-webhook").checked) triggers.push({ kind: "webhook" });
  const spec = {
    name: $("r-name").value.trim(),
    task: $("r-task").value.trim(),
    triggers,
    workspace: $("r-workspace").value.trim() || null,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    config: { approval: $("r-approval").value, memory: $("r-memory").checked,
      ...(checkedServices("r-services").length ? { connectors: checkedServices("r-services") } : {}) },
    approval_ttl_s: Math.max(1, Number($("r-ttl").value)) * 60,
  };
  try {
    await api("/api/routines", { method: "POST", body: spec });
    resetRoutineForm();
    $("routine-hint").textContent = `Saved ${spec.name}.`;
    loadRoutines();
  } catch (err) {
    $("routine-hint").textContent = err.message;
  }
}

// ------------------------------------------------------------------- memory
async function loadMemory() {
  const list = $("memory-list");
  let items;
  try {
    items = await api("/api/memory");
  } catch (err) {
    list.replaceChildren(el("li", { class: "empty-line", text: err.message }));
    return;
  }
  $("memory-form").hidden = S.readOnly;
  list.replaceChildren(...items.map((m) => {
    const source = m.source || {};
    const origin = source.session_id
      ? el("a", { href: `#/s/${source.session_id}`, text: `saved by the agent in ${source.session_id}` })
      : el("span", { text: "added by you" });
    const act = (path, method) => async () => {
      await api(`/api/memory/${m.id}${path}`, { method, body: method === "POST" ? {} : undefined }).catch(() => {});
      loadMemory();
    };
    return el("li", {},
      el("p", { class: "text", text: m.text }),
      el("div", { class: "meta" }, origin, el("span", { text: when(m.created_at) }),
        m.unreviewed ? el("span", { class: "unreviewed", text: "Unreviewed: saved after reading untrusted content" }) : null,
        S.readOnly || !m.unreviewed ? null : el("button", { class: "btn-link", type: "button", text: "Keep", onclick: act("/review", "POST") }),
        S.readOnly ? null : el("button", { class: "btn-link", type: "button", text: "Forget", onclick: act("", "DELETE") })));
  }));
  if (!items.length) list.append(el("li", { class: "empty-line", text: "Nothing remembered yet." }));
}

async function addMemory(event) {
  event.preventDefault();
  const text = $("memory-text").value.trim();
  if (!text) return;
  await api("/api/memory", { method: "POST", body: { text } }).catch(() => {});
  $("memory-text").value = "";
  loadMemory();
}

// ----------------------------------------------------------------- services
function renderServices(id) {
  const box = $(id);
  box.hidden = S.connectors.length === 0;
  box.replaceChildren(box.querySelector("legend"), ...S.connectors.map((name) =>
    el("label", {}, el("input", { type: "checkbox", value: name }), name)));
}

function checkedServices(id) {
  return [...$(id).querySelectorAll("input:checked")].map((input) => input.value);
}

function setServices(id, names) {
  $(id).querySelectorAll("input").forEach((input) => (input.checked = names.includes(input.value)));
}

// ---------------------------------------------------------------- approvals
async function loadApprovals() {
  try {
    renderApprovals(await api("/api/approvals"));
  } catch (err) {
    $("approval-list").replaceChildren(el("li", { class: "empty-line", text: err.message }));
  }
}

function renderApprovals(items) {
  const list = $("approval-list");
  list.replaceChildren();
  if (!items.length) list.append(el("li", { class: "empty-line", text: "Nothing is waiting for you." }));
  for (const a of items) {
    const item = el("li", { class: "approval-item" });
    const decide = (approved, always = false) => async () => {
      item.querySelectorAll("button").forEach((b) => (b.disabled = true));
      try {
        await api(`/api/sessions/${a.session_id}/approvals`, { method: "POST", body: { call_id: a.call_id, approved, always } });
      } catch (err) {
        item.append(el("p", { class: "note-line red", text: err.message }));
      }
      setTimeout(refreshApprovalCount, 300);
    };
    item.append(
      el("div", { class: "call-head" }, el("span", { class: "call-name", text: a.tool }),
        el("span", { class: "call-args", text: shortArgs(a.arguments) })),
      el("pre", { text: JSON.stringify(a.arguments, null, 2) }),
      el("p", { class: "why", text: approvalWhy(a.rule, a.reason) }),
      el("div", { class: "from" },
        el("span", { text: a.routine ? `From routine ${a.routine}` : "From a session" }),
        el("a", { href: `#/s/${a.session_id}`, text: "Open the run" }),
        el("span", { text: `Asked ${when(a.requested_at)}` }),
        a.expires_at ? el("span", { class: "expiry", text: `Skipped at ${clockTime(a.expires_at)} if nobody answers` }) : null),
      S.readOnly ? null : el("div", { class: "actions" },
        el("button", { class: "btn btn-primary", type: "button", text: "Allow once", onclick: decide(true) }),
        a.rule === "confirm" ? el("button", { class: "btn", type: "button", text: `Always allow ${a.tool}`,
          onclick: decide(true, true) }) : null,
        el("button", { class: "btn btn-danger", type: "button", text: "Deny", onclick: decide(false) })));
    list.append(item);
  }
  loadRules();
}

async function loadRules() {
  const list = $("rule-list");
  let rules = [];
  try {
    rules = await api("/api/rules");
  } catch (_) { /* leave the list as it is */ }
  list.replaceChildren(...rules.map((r) => el("li", {},
    el("span", { class: "text", text: r.text }), r.note ? el("span", { class: "note", text: r.note }) : null,
    S.readOnly ? null : el("button", { class: "btn-link", type: "button", text: "Remove", onclick: async () => {
      await api(`/api/rules/${r.id}`, { method: "DELETE" }).catch(() => {});
      loadRules();
    } }))));
  if (!rules.length) list.append(el("li", { class: "empty-line", text: "No standing rules." }));
}

// ---------------------------------------------------------------------- boot
$("routine-form").addEventListener("submit", saveRoutine);
$("memory-form").addEventListener("submit", addMemory);
$("routine-cancel").addEventListener("click", resetRoutineForm);
$("r-when").addEventListener("change", setWhenFields);
$("r-approval").addEventListener("change", setWhenFields);
window.addEventListener("hashchange", route);
window.addEventListener("popstate", route);
$("composer").addEventListener("submit", submitComposer);
$("composer-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("composer").requestSubmit();
});
$("new-task").addEventListener("click", showNewTask);
$("stop").addEventListener("click", async () => {
  if (S.current) await api(`/api/sessions/${S.current}/stop`, { method: "POST", body: {} }).catch(() => {});
});
$("resume").addEventListener("click", async () => {
  if (S.current) await api(`/api/sessions/${S.current}/resume`, { method: "POST", body: {} }).catch(() => {});
});

(async function boot() {
  const config = await api("/api/config").catch(() => ({}));
  S.readOnly = Boolean(config.read_only);
  S.connectors = config.connectors || [];
  renderServices("opt-services");
  renderServices("r-services");
  if (S.readOnly) {
    $("composer").hidden = true;
    $("new-task").hidden = true;
    $("intro").querySelector("h2").textContent = "Recorded sessions";
    $("intro").querySelector("p").textContent = "This server is read-only. Pick a session on the left to replay it step by step.";
  }
  await loadSessions();
  setWhenFields();
  if (location.hash.startsWith("#/s/") || ["#/routines", "#/approvals", "#/memory"].includes(location.hash)) route();
  else showNewTask();
  refreshApprovalCount();
  setInterval(refreshApprovalCount, 10000);
})();
