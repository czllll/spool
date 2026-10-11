# Spool

**An agent harness written from scratch, and what it measured.**

A harness is the code around a model that turns it into an agent: the loop that calls the model and
runs tools, the context it sends, the permissions it enforces, the state it keeps, and how it
recovers when the process dies. Spool is a small, readable one, about 9,000 lines of Python with no
agent framework and 208 offline tests, built to understand those mechanisms by measuring them.
Every session is an append-only event log, and every number below comes from evals run on a local
open model.

![Spool's web console showing a recorded session: the step-by-step log in the middle, cache use per step and the agent's folder on the right](docs/images/web.png)

## What it measured

All numbers come from Qwen3.5-4B (Q4_K_M) served by llama.cpp on an Apple M1 laptop (16 GB), three
concurrent sessions, three trials per task. They compare harness configurations on one small model;
they say nothing about frontier-model capability. `scripts/run_evals.sh` regenerates them; the full
report is [evals/results/REPORT.md](evals/results/REPORT.md), the headline tables are
[evals/results/HEADLINE.md](evals/results/HEADLINE.md), and the failure analysis is
[docs/notes/failures.md](docs/notes/failures.md).

| experiment | setup | result |
|---|---|---|
| Baseline | 12 core tasks (files, code, data), 15-minute budget | 92% success (95% CI 78–97%), pass^3 92%, 77% of prompt tokens served from cache |
| E1 prefix stability | same tasks, plus the wall clock at the top of the system prompt | cache reuse 77% → 8%, prefill per call 6.0 s → 20.6 s, median task time 272 s → 507 s, success 92% → 86% (5 timeouts) |
| E2 context strategy | 4 long tasks, 40-minute budget | full history 67%, summarize 58%, fixed-threshold offload 33%, fold 25%; budget-aware offload (now the default) 67% |
| E4 crash recovery | SIGKILL at a chosen point (including after a message was sent but before it was logged), resume in a new process | 8/8 trials finished and passed; exactly one message sent in every side-effect trial |
| E5 side effects | 8 tasks acting on mail, calendar and shop services through MCP; a simulated user answers approvals | 21/24 passed (88%, CI 69–96%). Of the 21 writes the services recorded: 0 without approval, 0 after an approval expired, 0 untracked, 0 duplicated |
| Replay | re-render every recorded request from the log and compare digests | 1,965 requests, 0 mismatches |

What the experiments changed in the harness:

- **E2 overturned a default.** Offloading every output above 3,000 characters lost all three
  `wiki-research` trials: the agent did read the saved files back (52 of 54), but paging through
  them used up the step limit. Prompts never passed ~8k tokens of a 32k window, so offloading bought
  nothing. Offloading is now budget-aware (`offload_when_over`): outputs stay inline until the
  projected prompt passes half the context budget. Re-measured: 67%, the same as full history, with
  `wiki-research` back to 3/3. (Failure 0004.)
- **A prompt fix did nothing; a harness fix worked.** `rename-function` failed 3/3 at the step limit
  with the work already done, because the model rewrote its plan after every edit. Asking it not to
  left plan updates at 8% of tool calls. Letting a few plan-only turns go uncharged
  (`free_plan_steps`) took the task to 3/3. (Failure 0002.)
- **The first real trial found a tool bug.** The model copied a separator space from `read_file`'s
  line-number column into an edit, and the whitespace-tolerant matcher pasted it back. `read_file`
  now uses the `cat -n` layout and fuzzy edits re-indent the replacement. (Failure 0001.)
- **E5 found a behaviour the side-effect checks cannot catch.** In `reschedule-conflict` (0/3) the
  agent saw the conflict, then booked the counter-proposal instead of proposing it, never sent the
  reply, and said it had. Every write was approved and recorded exactly once; the result was still
  wrong. The hypothesis (untested) is that the approval note in the send tool's description steers a
  small model toward a tool that needs no approval. (Failure 0005, open.)

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

## How it works

Two decisions carry the design:

1. **The session is the log** ([ADR 0001](docs/adr/0001-event-log-as-source-of-truth.md)). Every step
   is appended to SQLite *before* it has any effect. Recovery, approvals, replay, forking and evals
   are all derived from that one log, and its append-only shape is the shape prompt caches need.
2. **The loop is a pure state machine** ([ADR 0002](docs/adr/0002-pure-decide-function.md)).
   `reduce(state, event)` and `decide(state, now)` have no IO; the runtime only performs the action
   `decide` names. Recovery is "replay, then ask `decide` again", and the decision table is
   unit-tested without a model.

```
   CLI / web console / eval runner
                 │
   ┌──────── Runtime: imperative shell ─────────────────────────┐
   │  pure loop: reduce(state, event), decide(state, now)        │
   │  actions: call model | authorize | run tool | wait | finish │
   └──┬──────────────┬───────────────┬──────────────┬───────────┘
 context engine   model gateway   policy engine   tools + sandbox + MCP
 (append-only,    (OpenAI-compat,  (Rule of Two,   (shell and files in
  offload, fold)   Anthropic)       approvals)      Seatbelt or Docker)
        │
   ┌────┴──────── event log (SQLite, append-only, fsync per event) ────┐
   │  recovery · replay/verify · fork · viewer · evals · SFT export    │
   └────────────────────────────────────────────────────────────────────┘
```

| mechanism | how | design note |
|---|---|---|
| Exactly-once side effects | a write-ahead `tool_started` event with an idempotency key; per tool: re-run, re-run with the same key (deduplicated by a ledger or by the service), or report "interrupted, effects unknown" | [ADR 0007](docs/adr/0007-crash-semantics-per-tool.md) |
| Cache-friendly context | rendering is a pure, append-only function of the log; budget-aware offload, reversible folding with `recall`, summarize as a baseline | [ADR 0003](docs/adr/0003-append-only-context-and-reversible-folding.md) |
| Prompt-injection blast radius | Meta's Rule of Two with taint tracking; the sandbox is what makes it enforceable | [ADR 0004](docs/adr/0004-rule-of-two-policy.md) |
| Sandboxes | macOS Seatbelt, hardened Docker, or a plain process | [ADR 0005](docs/adr/0005-sandbox-backends.md) |
| Model gateway | OpenAI-compatible client with a hand-written streaming parser (llama.cpp, DashScope/Qwen, DeepSeek, Moonshot, OpenRouter, OpenAI); Anthropic through the official SDK; retries with jitter; session-to-slot pinning | [design.md](docs/design.md) |
| MCP | a hand-written client and server for both protocol eras (the 2025-11-25 handshake and the 2026-07-28 stateless envelope), interop-tested in both directions against the official Python SDK; tool lists are snapshotted into the log | [ADR 0009](docs/adr/0009-connectors-over-mcp.md) |
| Evals | outcome-graded tasks, pass@k and pass^k, Wilson intervals, a failure taxonomy, ablation configs, resumable runs, crash mode (real SIGKILL), and an audit of side effects from the services' own records | [ADR 0006](docs/adr/0006-outcome-graded-evals.md) |

More in [docs/design.md](docs/design.md).

## Explorations

Built while exploring what long-running agents that act for a person need. Each is tested, none is
the point of the project, and none has been used for real work:

- **Routines**: cron, interval, new-file and webhook triggers. Each scheduled time fires exactly once,
  even with two schedulers or a crash between "fired" and "started"; approvals asked by unattended
  runs expire instead of running late ([ADR 0008](docs/adr/0008-routines-are-event-streams.md)).
- **Connectors**: MCP tools whose policy is derived from their annotations, with demo mail, calendar
  and shop services (MCP servers over SQLite) that E5 runs against.
- **Standing approval rules**: always allow, always ask, never; an "allow" never overrides the Rule
  of Two.
- **Memory** with provenance: a memory written after reading untrusted content stays unreviewed and
  taints any later session that is shown it.
- **Agent Skills** (SKILL.md folders) loaded on demand. An A/B suite exists (`evals/tasks/skills`)
  but has not been run yet.
- **Web console**: start tasks, follow every step live over SSE, approve, branch, resume.

## Quick start

```bash
uv venv --python 3.12 && uv pip install -e '.[dev,web]'
scripts/serve_local_model.sh &        # Qwen3.5-4B (Q4_K_M) via llama.cpp on :8089; downloads on first run

spool run "The tests in test_stats.py fail; fix stats.py" -w path/to/project
spool sessions                        # sessions and their status
spool show <session>                  # the event timeline
spool view <session> -o session.html  # self-contained HTML: steps, tool output, cache use per step
spool verify <session>                # re-render every request from the log, compare digests
spool fork <session> --at 12 --set context_strategy=fold --run

spool run "..." --approval human      # stops at actions that need approval
spool approve <session> <call_id>     # or --deny; then:
spool resume <session>

spool run "..." --model dashscope:qwen-plus          # any OpenAI-compatible provider
spool run "..." --model anthropic:claude-opus-5-5    # uv pip install -e '.[anthropic]'

spool web                             # the console on http://127.0.0.1:8765
scripts/run_evals.sh                  # every number in evals/results/REPORT.md
```

## Layout

```
spool/
  events.py store.py          event vocabulary, SQLite log with optimistic concurrency
  state.py decide.py          pure reducer and decision function
  runtime.py                  imperative shell: actions, write-ahead, recovery, sub-agents
  context.py replay.py        rendering, offload/fold/summarize, render verification
  policy.py                   Rule of Two, per-tool confirmation, standing rules
  tools/ sandbox/             built-in tools, schema validation, path jail, side-effect ledger; sandboxes
  models/                     neutral types, OpenAI-compatible and Anthropic adapters, SSE, retries
  mcp/ connectors.py          MCP client and server; connector tools and their policy
  eval/ services/             task format, runner, metrics, reports; demo services and the side-effect audit
  routines.py cron.py rules.py memory.py skills.py   explorations (see above)
  web/ cli.py                 console (Starlette, plain HTML/CSS/JS) and command line
evals/  build_tasks.py tasks/ (core, long, actions, skills) configs/ results/
docs/   design.md adr/ notes/failures.md
tests/  208 tests, offline
```

## Testing

`pytest` runs offline in a few minutes. It covers the decision table, store concurrency and
durability (a real `kill -9`), crash recovery at every point, prefix stability, offloading, folding
and `recall`, compaction, loop warnings, the policy matrix, path-jail escapes, the ledger under
concurrent processes, Seatbelt and Docker confinement (skipped when unavailable), streaming parsing
with fragmented tool calls, retries, MCP in both protocol eras (against the official SDK in both
directions when it is installed), the eval runner, the web app and the CLI. Every eval task's
verifier is tested as well: an idle agent must fail and a reference agent must pass, the latter
through the real runner, services and approver, once with a real `SIGKILL` right after an order is
placed.

## Limitations

- Results come from one 4B model on a laptop. They compare harness configurations, not frontier
  capability, and no frontier model was run.
- The E5 services are local stand-ins (SQLite behind MCP) and the tasks were written by the author;
  they test the harness's guarantees, not a product.
- Single node: one writer per session. Firing routines is safe with several schedulers on one store,
  but running sessions is not leased.
- Forks copy the workspace as it is now, not as it was at the fork point.
- The Anthropic adapter is tested against a fake client only.
- The policy limits the damage an injection can do; it does not detect injections.

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

## License

MIT
