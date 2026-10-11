"""Search-and-replace file editing with a whitespace-tolerant fallback."""

from __future__ import annotations

import difflib


class EditError(ValueError):
    pass


def apply_edit(text: str, old: str, new: str, *, replace_all: bool = False) -> tuple[str, str]:
    """Return (new_text, message) or raise EditError with advice for the model.

    1. Exact match: must be unique unless replace_all.
    2. Already applied: `old` absent but `new` present -> no-op. This makes the
       edit idempotent, so re-running it after a crash is safe.
    3. Fuzzy match: the same lines ignoring leading/trailing whitespace, if that
       identifies exactly one block. Small models often get indentation wrong.
    """
    if old == "":
        raise EditError("old_string is empty; use write_file to create or overwrite a file")
    if old == new:
        raise EditError("old_string and new_string are identical; nothing to change")
    count = text.count(old)
    if count == 1 or (count > 1 and replace_all):
        return text.replace(old, new), f"replaced {count} occurrence{'s' if count > 1 else ''}"
    if count > 1:
        raise EditError(
            f"old_string occurs {count} times; include more surrounding lines so it is unique, or set replace_all=true"
        )
    if new and new in text:
        return text, "no change: new_string is already present and old_string is not (edit already applied)"

    matched = _fuzzy_block(text, old)
    if matched is not None:
        start, end = matched
        replacement = _reindent(new, old, text[start:end])
        return (text[:start] + replacement + text[end:],
                "replaced 1 occurrence (matched ignoring indentation; the replacement was re-indented to match the file)")

    if new.strip() and _fuzzy_block(text, new) is not None:
        # Same idempotency rule as above, for an edit that was applied through the fuzzy path.
        return text, "no change: new_string is already present (ignoring indentation) and old_string is not"

    hint = ""
    first = next((line for line in old.splitlines() if line.strip()), "")
    if first:
        close = difflib.get_close_matches(first.strip(), [l.strip() for l in text.splitlines()], n=2, cutoff=0.6)
        if close:
            hint = " Closest existing line(s): " + " | ".join(repr(c) for c in close)
    raise EditError("old_string not found in the file. Re-read the file and copy the exact text." + hint)


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _first_nonblank(text: str) -> str:
    return next((line for line in text.splitlines() if line.strip()), "")


def _reindent(new: str, old: str, actual: str) -> str:
    """Shift `new` by the indentation difference between `old` and the text it matched.

    A model that copies one extra leading space from numbered file output, or
    drops a block's common indentation, gets its replacement moved to where the
    matched lines actually sit instead of being pasted verbatim.
    """
    old_ind, act_ind = _indent(_first_nonblank(old)), _indent(_first_nonblank(actual))
    if old_ind == act_ind:
        return new
    lines = []
    for line in new.split("\n"):
        if line.strip() and line.startswith(old_ind):
            lines.append(act_ind + line[len(old_ind):])
        else:
            lines.append(line)
    return "\n".join(lines)


def _fuzzy_block(text: str, old: str) -> tuple[int, int] | None:
    lines = text.splitlines(keepends=True)
    want = [l.strip() for l in old.strip("\n").splitlines()]
    if not want:
        return None
    hits: list[tuple[int, int]] = []
    for i in range(len(lines) - len(want) + 1):
        if all(lines[i + j].strip() == want[j] for j in range(len(want))):
            start = sum(len(l) for l in lines[:i])
            end = start + sum(len(l) for l in lines[i : i + len(want)])
            # keep the final newline of the block outside the replacement
            if lines[i + len(want) - 1].endswith("\n"):
                end -= 1
            hits.append((start, end))
    return hits[0] if len(hits) == 1 else None
