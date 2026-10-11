"""Tool specs, argument validation and workspace path checks."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

from ..config import AgentConfig

# Rule-of-Two properties (Meta, "Agents Rule of Two", 2025-10-31): a session should not
# combine all three without a human in the loop.
PROP_UNTRUSTED = "untrusted_input"   # [A] processes content an attacker could have written
PROP_SENSITIVE = "sensitive_data"    # [B] reads private data or secrets
PROP_EXTERNAL = "external_comm"      # [C] changes state outside the sandbox or sends data out
ALL_PROPERTIES = (PROP_UNTRUSTED, PROP_SENSITIVE, PROP_EXTERNAL)

INTERNAL_DIR = ".spool"


@dataclass
class ToolResult:
    ok: bool = True
    output: str = ""
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)  # merged into the TOOL_COMPLETED event

    @classmethod
    def fail(cls, error: str, output: str = "", **data: Any) -> "ToolResult":
        return cls(ok=False, output=output, error=error, data=data)


@dataclass
class ToolContext:
    workspace: Path
    sandbox: Any
    session_id: str
    call_id: str
    idem_key: str
    config: AgentConfig
    ledger_dir: Path
    fixtures: Mapping[str, Any]
    history: Callable[[str], Mapping[str, Any] | None]
    now: Callable[[], float]
    spawn: Callable[[str, int], Awaitable[dict[str, Any]]] | None = None


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    properties: frozenset[str] = frozenset()  # static Rule-of-Two properties
    path_arg: str | None = None              # argument naming a workspace path
    path_reads: bool = True                  # the path's content reaches the model (classified per call)
    idempotent: bool = True                  # safe to run again if a crash interrupted it
    ledgered: bool = False                   # external side effect, deduplicated by idempotency key

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


class ToolRegistry:
    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                raise ValueError(f"duplicate tool {spec.name}")
            self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def schemas(self) -> list[dict[str, Any]]:
        # Sorted by name so the tools block of every request is byte-identical.
        return [self._specs[name].schema() for name in sorted(self._specs)]

    def subset(self, names: Iterable[str] | None) -> "ToolRegistry":
        if names is None:
            return self
        wanted = set(names)
        unknown = wanted - set(self._specs)
        if unknown:
            raise ValueError(f"unknown tools: {sorted(unknown)}")
        return ToolRegistry(spec for name, spec in self._specs.items() if name in wanted)


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_args(schema: Mapping[str, Any], args: Any, path: str = "") -> list[str]:
    """Validate against the JSON-schema subset our tools use.

    Covers type, required, properties, items, enum and additionalProperties.
    Errors are phrased for the model: they go back to it as the tool result.
    """
    errors: list[str] = []
    where = path or "arguments"
    expected = schema.get("type")
    if expected:
        allowed = _TYPES.get(expected, ())
        ok = isinstance(args, allowed) and not (expected in ("integer", "number") and isinstance(args, bool))
        if not ok:
            return [f"{where} must be of type {expected}, got {type(args).__name__}"]
    if "enum" in schema and args not in schema["enum"]:
        errors.append(f"{where} must be one of {schema['enum']}, got {args!r}")
    if expected == "object" and isinstance(args, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in args:
                errors.append(f"missing required argument {req!r}")
        for key, value in args.items():
            if key in props:
                errors.extend(validate_args(props[key], value, f"{path}.{key}" if path else key))
            elif schema.get("additionalProperties") is False:
                errors.append(f"unexpected argument {key!r}")
    if expected == "array" and isinstance(args, list) and "items" in schema:
        for i, item in enumerate(args):
            errors.extend(validate_args(schema["items"], item, f"{where}[{i}]"))
    return errors


class PathError(ValueError):
    pass


def resolve_path(workspace: Path, path: str) -> Path:
    """Resolve `path` inside the workspace, following symlinks; refuse escapes."""
    if not isinstance(path, str) or not path.strip():
        raise PathError("path must be a non-empty string")
    ws = os.path.realpath(workspace)
    candidate = path if os.path.isabs(path) else os.path.join(ws, path)
    resolved = os.path.realpath(candidate)
    if resolved != ws and not resolved.startswith(ws + os.sep):
        raise PathError(f"{path!r} is outside the workspace")
    return Path(resolved)


def relative_parts(workspace: Path, resolved: Path) -> tuple[str, ...]:
    return Path(os.path.relpath(resolved, os.path.realpath(workspace))).parts


def classify_path(workspace: Path, path: str, config: AgentConfig) -> frozenset[str]:
    """Rule-of-Two properties of touching `path`; unresolvable paths classify as nothing."""
    try:
        parts = relative_parts(workspace, resolve_path(workspace, path))
    except PathError:
        return frozenset()
    if not parts or parts == (".",):
        return frozenset()
    props = set()
    if parts[0] in config.sensitive_paths:
        props.add(PROP_SENSITIVE)
    if parts[0] in config.untrusted_paths:
        props.add(PROP_UNTRUSTED)
    return frozenset(props)
