# Spool

**A self-hosted, always-on agent, built on an event-sourced runtime.** Routines start tasks on
their own (on a schedule, when files arrive, or from a webhook); the agent acts on your services
through MCP connectors; anything that sends, buys or deletes waits for your approval, and an
approval nobody answers expires instead of running late. Every step is appended to a durable log
*before* it has any effect, so crash recovery, exactly-once actions, replay, forking, context
management and evaluation all come from that one log.

Between August and September 2026, xAI (Grok Bot), Meta (the Muse agent), Manus and Qwen's task
assistant all moved to this shape: an agent that keeps working after you leave, signs into your
apps, and comes back only when it needs a decision. The hard parts are not the model calls. A
background run must start exactly once per scheduled time even if the scheduler crashes; an email
must go out once even if the process dies right after sending it; an approval asked at 3 a.m. must
not be granted at 3 p.m.; every action must be explainable afterwards; and a prompt injection in
an email must not be able to make the agent send your data out. Spool is a small, readable
implementation of that layer: about 8,500 lines of Python, no agent framework, 187 tests, and evals
run on a local open model.

The product is a local web app: give the agent a task or a routine, watch each step as it happens,
answer approvals from one inbox, browse its folder, and branch from any step.

![Spool web app showing a recorded session: the step-by-step log in the middle, cache use per step and the agent's folder on the right](docs/images/web.png)

## What it does

| capability | how | evidence |
|---|---|---|
| Crash-safe, exactly-once side effects | write-ahead `tool_started` + idempotency keys + a side-effect ledger; per-tool crash semantics ([ADR 0007](docs/adr/0007-crash-semantics-per-tool.md)) | crash tests at every point and a real `kill -9` test; crash eval with real model |
| Human approval that survives restarts | `ask` verdicts suspend the session; `spool approve` + `spool resume` | `tests/test_runtime.py`, `tests/test_eval_and_tools_cli.py` |
| Exact replay | requests are a pure function of the log; each response stores per-part request digests; `spool verify` re-renders and compares | run on every eval trial (see results) |
| Cache-friendly context | append-only rendering, large outputs offloaded to files, reversible folding (`recall`), summarize baseline ([ADR 0003](docs/adr/0003-append-only-context-and-reversible-folding.md)) | E1 and E2 below |
| Prompt-injection blast radius | Meta's Rule of Two with taint tracking; the sandbox makes it enforceable ([ADR 0004](docs/adr/0004-rule-of-two-policy.md)) | `tests/test_policy.py`, `tests/test_runtime.py` |
| Sandboxes | macOS Seatbelt, hardened Docker, or plain process ([ADR 0005](docs/adr/0005-sandbox-backends.md)) | `tests/test_sandbox.py` |
| Model gateway | OpenAI-compatible adapter with a hand-written streaming parser (llama.cpp, DashScope/Qwen, DeepSeek, Moonshot, OpenRouter, OpenAI); Anthropic via the official SDK; retries with jitter; session-to-slot pinning | `tests/test_models.py` |
| Evals | outcome-graded tasks, pass@k / pass^k, failure taxonomy, ablation configs, resumable runs ([ADR 0006](docs/adr/0006-outcome-graded-evals.md)) | `evals/`, `scripts/run_evals.sh` |
| Routines | cron (DOM/DOW and daylight-saving rules), interval, new-file and webhook triggers; each scheduled time fires exactly once, even with a crash between "fired" and "started" or two schedulers on one store; misfire and overlap policies ([ADR 0008](docs/adr/0008-routines-are-event-streams.md)) | `tests/test_routines.py`, `tests/test_cron.py` |
| Approvals that expire | unattended runs ask with a deadline; `decide(state, now)` expires it; a late "allow" is refused and the action never runs | `tests/test_approval_expiry.py` |
| Connectors over MCP | hand-written client and server for both protocol eras (2025-11-25 handshake, 2026-07-28 stateless), interop-tested against the official SDK; tool lists snapshotted into the log; Rule-of-Two properties, always-ask and crash behaviour derived from MCP annotations; remote writes exactly once via a hidden idempotency key ([ADR 0009](docs/adr/0009-connectors-over-mcp.md)) | `tests/test_mcp.py`, `tests/test_connectors.py` |
| Actions, audited from outside | demo mail, calendar and shop services (MCP over SQLite); after every eval trial their records are matched against the log: writes without approval, after expiry, untracked, duplicated | `tests/test_actions_suite.py`, E5 below |
| Product UI | local web app (`spool web`): tasks and routines, every step live, one approvals inbox, branch, resume, browse the folder; the live view is the event log streamed over SSE, so reconnects and server restarts lose nothing | `tests/test_web.py` |
| Also | sub-agents with taint roll-up, `sleep` + `spool tick`, HTML session viewer, SFT export, a Gym-style RL environment | tests |

## Quick start

```bash
uv venv --python 3.12 && uv pip install -e '.[dev,web]'
scripts/serve_local_model.sh &        # Qwen3.5-4B (Q4_K_M) via llama.cpp on :8089; downloads on first run

spool web                             # http://127.0.0.1:8765: tasks, live steps, approvals, branching

spool run "The tests in test_stats.py fail; fix stats.py" -w path/to/project
spool sessions                        # list sessions and their status
spool show <session>                  # the event timeline
spool view <session> -o session.html  # self-contained HTML: steps, tool output, cache use per step
spool verify <session>                # re-render every request from the log, compare digests
spool fork <session> --at 12 --set context_strategy=fold --run

spool run "..." --approval human      # stops at actions that need approval
spool approve <session> <call_id>     # or --deny; then:
spool resume <session>

spool run "..." --model dashscope:qwen-plus          # any OpenAI-compatible provider
spool run "..." --model anthropic:claude-opus-5-5    # pip install -e '.[anthropic]'

export SPOOL_CONNECTORS=examples/connectors.toml   # demo mail, calendar and shop (MCP servers on SQLite)
spool connectors                      # their tools, and the policy Spool derives for each
spool run "Reply to Amy that Thursday works" --connector mail --approval human

spool routine add morning-inbox --cron "0 9 * * 1-5" --connector mail \
      --task "Read new mail and draft replies where one is needed"
spool routine add receipts --watch inbox --pattern "*.pdf" --task "File these receipts: {files}"
spool routine list                    # triggers, next run, last run
spool daemon                          # fire routines and run due sessions (or `spool web`, or cron + `spool tick`)

scripts/run_evals.sh                  # every number in evals/results/REPORT.md
```

## Architecture

```
  web app (SSE, routines, approvals)   CLI / daemon / tick   eval runner (+ simulated approver)
                 \                          |                    /
     scheduler: routines ─ due(state, now, files) ─ write-ahead ROUTINE_FIRED ─ start session
                 \                          |                    /
        ┌──────── Runtime: imperative shell ────────────────────────┐
        │  Agent loop (pure): reduce(state, event), decide(state,now)│
        │  executor: call model | authorize | run tool | wait | end │
        └──┬──────────────┬───────────────┬───────────────┬────────┘
     context engine   model gateway   policy engine   tools + sandbox     connectors (MCP)
     (append-only,    (OpenAI-compat,  (Rule of Two,   (bash, files;      (snapshot in log;
      offload, fold)   Anthropic)       confirm, TTL)   Seatbelt/Docker)   idempotency keys)
           │                                                                  │
   ┌───────┴───── event log (SQLite, append-only, fsync per event) ─────┐   mail · calendar ·
   │  recovery · replay/verify · fork · HTML viewer · evals · SFT export │   shop · any MCP server
   └─────────────────────────────────────────────────────────────────────┘
```

Two decisions carry the design:

1. **The session is the log** ([ADR 0001](docs/adr/0001-event-log-as-source-of-truth.md)). One
   decision gives recovery, audit, replay and forking, and the log's append-only shape is the shape
   prompt caches need.
2. **The loop is a pure state machine** ([ADR 0002](docs/adr/0002-pure-decide-function.md)).
   Recovery is "replay, then ask `decide` again"; the decision table is unit-tested without a model.

More in [docs/design.md](docs/design.md).

## Results

All numbers below come from Qwen3.5-4B (Q4_K_M) served by llama.cpp on an Apple M1 laptop
(16 GB), three concurrent sessions, three trials per task. They compare harness configurations on
one small model; they say nothing about frontier-model capability. `scripts/run_evals.sh`
regenerates them; the full report is [evals/results/REPORT.md](evals/results/REPORT.md) and the
failure analysis is [docs/notes/failures.md](docs/notes/failures.md).

| experiment | setup | result |
|---|---|---|
| Baseline | 12 core tasks, 15-minute budget | 92% success (95% CI 78–97%), pass^3 92%, 77% of prompt tokens served from cache |
| E1 prefix stability | same, plus the wall clock at the top of the system prompt | cache reuse 77% → 8%, prefill per call 6.0 s → 20.6 s, median task time 272 s → 507 s, success 92% → 86% (5 timeouts) |
| E4 crash recovery | SIGKILL at a chosen point (incl. after a message was sent but before it was logged), resume in a new process | 8/8 trials finished and passed; exactly one message sent in every side-effect trial |
| Replay | re-render every recorded request from the log | 1,869 requests, 0 digest mismatches |
| E2 context strategy | 4 long tasks, 40-minute budget | full history 67%, summarize 58%, fixed-threshold offload 33%, fold 25%; budget-aware offload (new default) 67% |

What the experiments changed in the harness:

- **E2 overturned a default.** Offloading every output above 3,000 characters lost all three
  `wiki-research` trials: the agent did read the saved files back (52 of 54), but paging through
  them used up the 20-step limit. Prompts never passed ~8k tokens of a 32k window, so offloading
  bought nothing. Offloading is now budget-aware (`offload_when_over`): outputs stay inline until
  the projected prompt passes half the context budget. Re-measured: 67% (8/12), the same as full
  history, with `wiki-research` back to 3/3. On these tasks no output was offloaded at all (13
  outputs over the threshold, none near the budget), so the new default behaves like full history
  until a prompt actually grows. (Failure 0004.)
- **A prompt fix did nothing; a harness fix worked.** `rename-function` failed 3/3 at the step limit
  with the work already done, because the model rewrote its plan after every edit. Asking it not to
  (baseline_v2) left plan updates at 8% of tool calls and success at 31/36. Letting a few plan-only
  turns go uncharged (`free_plan_steps`, baseline_v3) took the task to 3/3; the core suite went to
  32/36, within noise of the baseline's 33/36, and none of the remaining failures had plan-only
  turns. (Failure 0002.)
- **The first real trial found a tool bug.** The model copied a separator space from `read_file`'s
  line-number column into an edit, and the whitespace-tolerant matcher pasted it back. `read_file`
  now uses the `cat -n` layout and fuzzy edits re-indent the replacement. (Failure 0001.)

Two serving-level observations from the same runs:

- **Hybrid-attention models cache differently.** Qwen3.5 mixes linear and full attention; llama.cpp
  can restore its recurrent state only at checkpoints. When the chat template re-renders an earlier
  assistant turn a few tokens differently from what was generated, the server falls back to the last
  checkpoint (or to zero), so 5% of follow-up requests recomputed the whole prompt even with a stable
  prefix. The harness sends tool-call arguments back as the exact strings the model produced, which
  removes one source of re-render drift.
- **Session affinity matters under concurrency.** Without pinning, a new session could take the slot
  of a session waiting on a tool and evict its cache. Each concurrent trial now holds one server slot
  (`id_slot`) for its whole run.

## Layout

```
spool/
  events.py store.py          event vocabulary, SQLite log with optimistic concurrency
  state.py decide.py          pure reducer and decision function
  runtime.py                  imperative shell: actions, write-ahead, recovery, sub-agents
  context.py replay.py        rendering, offload/fold/summarize, render verification
  policy.py                   Rule of Two, confirm
  routines.py cron.py         routines, triggers, the scheduler; cron parsing
  mcp/ connectors.py          MCP client and server (both eras); connector tools and their policy
  services/                   demo MCP services (mail, calendar, shop) and the side-effect audit
  tools/                      built-in tools, schema validation, path jail, edit, side-effect ledger
  sandbox/                    process, Seatbelt, Docker
  models/                     neutral types, OpenAI-compatible + Anthropic adapters, SSE, retries,
                              pricing, scripted models, cassettes, slot pinning
  eval/                       task format, runner (incl. crash mode), metrics, reports
  viewer.py export.py env.py  HTML timeline, SFT export, Gym-style environment
  web/                        product UI: Starlette app (runs, approvals, SSE log tail) + static page, no build step
  cli.py
evals/  build_tasks.py tasks/ (core, long, actions) configs/ results/
examples/  connectors.toml seeds/   demo services for `spool web`
docs/   design.md adr/
tests/  187 tests, offline
```

## Testing

`pytest` runs offline in under a minute. It covers the decision table, store concurrency and
durability (a real `kill -9`), crash recovery at every point, prefix stability, offloading, folding
and `recall`, compaction, loop warnings, the policy matrix, path jail escapes, edit fallbacks, the
ledger under concurrent processes, Seatbelt and Docker confinement (skipped when unavailable),
streaming parsing with fragmented tool calls, retries, the Anthropic conversion, cassettes, the
eval runner, the RL environment, the web app (an approval round trip over HTTP and SSE replay
from `Last-Event-ID`) and the CLI. Routines are tested with a fake clock (exactly-once firing,
racing schedulers, a crash between fire and start, misfire and overlap, webhooks, new files) and
so are expiring approvals; MCP is tested in both protocol eras, against the official SDK in both
directions, with timeouts and a crashing server. Every eval task's verifier is tested too: the
untouched workspace must fail and a reference solution must pass; for the actions suite the
reference agents run through the real runner, services, approver and audit, one of them with a
real `SIGKILL` right after an order is placed.

## Limitations

- Single node: one writer per session. Firing routines is safe with several schedulers on one store,
  but running sessions is not leased yet, so two workers could resume the same interrupted session.
- Connectors speak stdio only (no Streamable HTTP or OAuth), and the demo services stand in for real
  accounts; any stdio MCP server can be plugged in, but credentials are that server's business.
- Forks copy the workspace as it is now, not as it was at the fork point.
- The Anthropic adapter is tested against a fake client only (no API key was available).
- The policy limits the damage an injection can do; it does not detect injections.
- Results come from a 4B model on a laptop; they compare configurations, not frontier capability.

## References

- Meta, [Agents Rule of Two](https://ai.meta.com/blog/practical-ai-agent-security/) (2025-10-31); Nasr et al., *The Attacker Moves Second* (arXiv, 2025-10-10)
- OpenAI, [Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop) (2026-01)
- Anthropic, [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) (2025-11-26); [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) (2026-01-09)
- Su et al., [ReFold: training-free reversible inter-turn context folding](https://arxiv.org/abs/2610.07863) (2026-10-06)
- Sun et al., [Scaling long-horizon LLM agent via context-folding](https://arxiv.org/abs/2510.11967) (2025-10-13)
- Zhang, Kraska, Khattab, [Recursive Language Models](https://arxiv.org/abs/2512.24601) (2025-12-31)
- Addy Osmani, [Agent harness engineering](https://addyosmani.com/blog/agent-harness-engineering/) (2026-04-19)
- [Terminal-Bench 2.0 and Harbor](https://www.tbench.ai/news/announcement-2-0) (2025-11)
- Yao et al., tau-bench (pass^k)
- [Model Context Protocol specification](https://modelcontextprotocol.io/specification) (2025-11-25 and 2026-07-28)
- xAI, [Introducing Grok Bot](https://x.ai/news/introducing-grok-bot) and its [approvals documentation](https://docs.x.ai/grok-bot/approvals-security-and-privacy) (2026-08)
