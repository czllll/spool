# ADR 0003: Append-only context, offloading, and reversible folding

Status: accepted

## Context

Prompt caches (provider caches, llama.cpp's KV cache) only reuse an exact prefix. OpenAI's
"Unrolling the Codex agent loop" and Manus's context-engineering notes both make the same point:
anything that rewrites earlier bytes (a timestamp in the system prompt, unsorted tool JSON, an
edited history) makes every request pay full prefill again. At the same time, long tasks overflow
any window if every tool output stays inline.

## Decision

The context engine (`spool/context.py`) renders a request as a pure function of the log, ordered
system → tools (sorted by name) → messages in event order. A new event only appends messages.
Three mechanisms keep the window bounded:

1. **Offload** (default): a tool output above `offload_threshold_chars` is written to
   `.spool/outputs/<call_id>.txt` and only a head/tail preview plus the path stays inline. The log
   keeps the full output. Decided once per output, so it never changes later bytes.
2. **Fold** (reversible): when the estimated prompt exceeds `compact_trigger x budget`, outputs of
   every step older than `keep_recent_steps` render as one-line stubs naming the call; `recall`
   brings any of them back from the log. Folding happens in chunks, so the prefix is rewritten once
   per chunk, not per step. This follows ReFold (arXiv 2610.07863): keep the full history, compress
   only the rendered view, and make every removal reversible.
3. **Summarize** (lossy baseline): older turns are replaced by a model-written summary, recorded as
   an event so replay renders the same text.

Every model response stores per-part digests of the request it answered (system, tools, each
message) and the timestamp used to render it. `spool verify` re-renders every request from the log
and compares digests; the eval harness runs this check on every trial.

## Consequences

- Prefix stability is a tested property (`tests/test_context.py`), not a hope.
- Folding and summarizing are history edits. On Anthropic models that bind thinking blocks to the
  exact history, use `offload` (append-only) or the API's server-side compaction instead.
- A finding from the local evals: on Qwen3.5 (hybrid linear/full attention) llama.cpp can only
  restore recurrent state at checkpoints. When the chat template re-renders an earlier assistant
  turn slightly differently from what was generated, the cache falls back to the last checkpoint
  instead of the last matching token, so even a stable prefix does not reach 100% reuse. The
  harness cannot fix the template, but it records `cache_n` per call so the effect is visible.

## Alternatives considered

- Summarize-only (common default): simple, but lossy and it rewrites the prefix every time.
- Retrieval over history: extra moving parts; recall-by-id covers the cases we need.
