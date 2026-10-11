# Failure log

Every failure found while running real models is written down here with its root cause and the
harness change it led to. Raw trajectories are next to this file.

## 0001: a separator space copied into an edit (config-edit, Qwen3.5-4B)

- **Seen in:** the first baseline trial of `core/config-edit`. The agent reported success; the
  verifier found `" port = 8080"` with a leading space.
- **Trajectory** (`failure-0001-config-edit.json`): `read_file` showed lines as `"   4  port = 8000"`
  (number, two spaces, content). The model called
  `edit_file(old_string=" port = 8000", new_string=" port = 8080")`, carrying one separator space
  into both strings. There was no exact match, the whitespace-tolerant fallback matched the line,
  and the replacement was pasted verbatim, leading space included.
- **Root cause:** two harness choices, not a model limitation alone: an ambiguous line-number
  layout, and a fuzzy matcher that ignored indentation when matching but not when replacing.
- **Fix:**
  1. `read_file` uses the `cat -n` layout (number, tab, content), which leaves no doubt where the
     content starts.
  2. When the fuzzy path matches, the replacement is re-indented by the difference between
     `old_string`'s indentation and the matched text's indentation.
  3. Re-running a fuzzy edit that already applied is a no-op, so crash recovery stays idempotent.
- **Regression tests:** `tests/test_tools.py::test_edit_fuzzy_reindents_the_replacement`.

## 0002: the plan eats the step budget (rename-function, Qwen3.5-4B)

- **Seen in:** `core/rename-function`, 0 of 3 baseline trials passed, all with status `max_steps`.
  In every one the verifier accepted the workspace: the work was done, the agent just never got to
  say so within 15 steps.
- **Trajectory:** 15 model turns = 4 reads, 5 edits, 1 test run and 5 `todo_write` calls. The model
  rewrote the plan after nearly every edit, as the system prompt invites ("write a short plan with
  todo_write and keep it updated").
- **Root cause:** a prompt instruction that is cheap for a large model costs a 4B model a full turn
  each time; the harness already re-states the plan every 4 tool calls, so the updates bought nothing.
- **Attempt 1, prompt (rejected):** `evals/configs/baseline_v2.toml` asks for a plan update only when
  an item is finished. On the 12 core tasks x 3 trials it changed nothing: `todo_write` stayed at 8%
  of all tool calls (15/178 before, 15/183 after), `rename-function` still failed 3 of 3 the same
  way, and overall success went from 33/36 to 31/36 (the two extra failures are miscounted totals
  where the model did arithmetic in its head; within noise for 36 trials). A 4B model does not
  reliably follow a soft instruction like this one.
- **Attempt 2, harness (kept):** `free_plan_steps`: up to N turns whose only tool calls are
  `todo_write` are not charged against `max_steps`; tokens and wall clock still count. With N = 5
  (`evals/configs/baseline_v3.toml`) `rename-function` went from 0/3 to 3/3, each run using 16-17
  turns of which 5 were plan-only. Overall core success was 32/36 against 33/36 for the baseline;
  the four v3 failures (two timeouts, two miscounted logs) are in trajectories with no plan-only
  turns, so the change could not have caused them; they are run-to-run noise of a sampled 4B model.
  N = 5 is now the default; recorded configs pin `free_plan_steps = 0`.

## 0003: long tasks hit the wall-clock budget, not a capability limit

- **Seen in:** 5 of 12 long-task trials in the first baseline run timed out at 15 minutes.
  `long/fix-calculator` was down to 1 failing test of 10 when it was cut off.
- **Root cause:** with three sessions sharing one laptop GPU, decoding runs at about 2 tokens/s per
  session; the model's 400-500-token reasoning turns take 3-5 minutes each.
- **Change:** long tasks get a 40-minute budget (`timeout_s = 2400`). The 15-minute results stay in
  `evals/results/baseline` and are reported as such.

## 0004: offloading too early costs steps (E2, wiki-research)

- **Seen in:** the context-strategy comparison on the long tasks. `long/wiki-research` passed 3/3 with
  full history and with summarize, and 0/3 with offload and with fold (both offload large outputs),
  all three ending at the 20-step limit without writing `answer.md`. The other three long tasks
  behaved about the same in every arm.
- **Trajectory:** with full history the agent fetched 4 pages and answered in 7-12 steps. With
  offload every page (4-6k characters, above the fixed 3,000-character threshold) became a preview
  plus a file path. The agent did follow up (52 of 54 offloaded outputs were read back), but paging
  through them took 9-14 `read_file` calls and it re-fetched pages it had already seen (6-10
  fetches), so the step budget ran out.
- **Root cause:** a fixed threshold offloads output even when the window has plenty of room. Peak
  prompts were about 8k tokens against a 32k window; context was never the binding constraint,
  steps were. Offloading only pays when it prevents an overflow.
- **Fix:** budget-aware offloading (`offload_when_over`, now 0.5 by default): an output above the
  threshold stays inline until the projected prompt would pass half of `context_budget_tokens`;
  outputs above `hard_cap_chars` are always offloaded. The decision is made once per output and
  stored in the event, so the prompt stays append-only. Measured with `evals/configs/ctx_adaptive.toml`.
  The recorded runs pin `offload_when_over = 0.0`, so they reproduce exactly.
- **Result (ctx_adaptive):** 8/12 (67%, 95% CI 39-86%), the same as full history; `wiki-research`
  3/3. Thirteen outputs exceeded the 3,000-character threshold and none was offloaded, because no
  prompt came near half the budget. The remaining failures are `fix-calculator` (two timeouts and
  one completed-but-wrong run, also the weak task with full history) and one `todo-audit` trial at
  the step limit. Kept as the default.
