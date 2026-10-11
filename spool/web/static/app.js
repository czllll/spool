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
  if (S.readOnly) {
    $("intro").hidden = false;
    $("ledger-head").hidden = true;
    $("entries").replaceChildren();
    return;
  }
  if (S.stream) S.stream.close();
  S.current = null;
  S.summary = null;
  location.hash = "";
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
  if (S.stream) S.stream.close();
  Object.assign(S, { current: id, lastSeq: -1, step: 0, summary: null, filesPath: ".", live: false });
  S.calls.clear();
  location.hash = `#/s/${id}`;
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
      fact("Approvals", APPROVAL_LABELS[cfg.approval] || cfg.approval),
      fact("Context", CONTEXT_LABELS[cfg.context_strategy] || cfg.context_strategy),
      fact("Folder", d.workspace),
    );
    if (d.parent && d.parent.kind === "fork") {
      facts.append(fact("Branched from", `${d.parent.session_id} at event ${d.parent.seq}`));
    }
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
    if (!call || d.verdict === "allow") return;
    if (d.verdict === "deny") {
      call.root.className = "call denied";
      call.state.textContent = "blocked";
      call.notes.append(el("p", { class: "note-line red", text: d.reason }));
    }
  },

  approval_requested(ev, d) {
    const call = S.calls.get(d.call_id);
    if (!call) return;
    call.root.className = "call awaiting";
    call.state.textContent = "needs your approval";
    const decide = (approved) => async () => {
      box.querySelectorAll("button").forEach((b) => (b.disabled = true));
      try {
        await api(`/api/sessions/${S.current}/approvals`, { method: "POST", body: { call_id: d.call_id, approved } });
      } catch (err) {
        box.append(el("p", { class: "note-line red", text: err.message }));
        box.querySelectorAll("button").forEach((b) => (b.disabled = false));
      }
    };
    const box = el("div", { class: "approval" },
      el("p", { text: "Allow this step? Together with what this session has already done, it would read untrusted "
        + "content, read protected data and send data out. That combination can leak data if the agent was misled "
        + "by something it read." }),
      S.readOnly ? null : el("div", { class: "actions" },
        el("button", { class: "btn btn-primary", type: "button", text: "Allow", onclick: decide(true) }),
        el("button", { class: "btn btn-danger", type: "button", text: "Deny", onclick: decide(false) })));
    call.approval = box;
    call.notes.append(box);
  },

  approval_decided(ev, d) {
    const call = S.calls.get(d.call_id);
    if (!call || !call.approval) return;
    call.approval.className = "approval decided";
    const who = d.by === "web" ? "You" : d.by;
    call.approval.replaceChildren(el("p", { text: `${who} ${d.approved ? "allowed" : "denied"} this step.` }));
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
    call.state.textContent = d.kind === "deduplicated" ? labels.deduplicated : (labels[kind] || kind);
    if (!d.ok && d.error && d.kind !== "denied") call.notes.append(el("p", { class: "note-line red", text: d.error }));
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

// ---------------------------------------------------------------------- boot
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
  if (S.readOnly) {
    $("composer").hidden = true;
    $("new-task").hidden = true;
    $("intro").querySelector("h2").textContent = "Recorded sessions";
    $("intro").querySelector("p").textContent = "This server is read-only. Pick a session on the left to replay it step by step.";
  }
  const rows = await loadSessions();
  const match = location.hash.match(/^#\/s\/(.+)$/);
  if (match) openSession(match[1]);
  else if (!rows.length) showNewTask();
  else showNewTask();
})();
