"""Agent Skills: folders of instructions (and scripts) the agent loads when a task calls for them.

    skills/meeting-scheduler/
        SKILL.md          ---\\n name: meeting-scheduler\\n description: ...\\n ---\\n instructions...
        reference.md      optional extra files, read on demand
        scripts/slots.py  optional scripts, run with bash

This is the open SKILL.md format (Anthropic, then an open standard used by
several agents). Loading is progressive, so a library of skills costs little
context:
  1. the session's system prompt lists each available skill's name and
     description only (snapshotted into the log before the first model call,
     like connector tools, so replays and caches do not depend on the disk);
  2. `load_skill(name)` returns the SKILL.md instructions and copies the skill
     folder into the workspace (`.skills/<name>/`), where `read_file` and the
     sandboxed `bash` can use its other files and scripts.

Skills are instructions the person chose to install, so loading one has no
Rule-of-Two properties; a skill from an untrusted source should be reviewed
like any other code before it is installed.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .util import digest

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_DESCRIPTION = 1024
MAX_BODY = 30_000
SKILLS_DIR = ".skills"


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    body: str
    metadata: Mapping[str, Any]

    def summary(self) -> dict[str, Any]:
        files = sorted(str(p.relative_to(self.path)) for p in self.path.rglob("*")
                       if p.is_file() and p.name != "SKILL.md" and not p.name.startswith("."))
        return {"name": self.name, "description": self.description, "files": files,
                "digest": digest([self.body, files], 12)}


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """The YAML front matter subset SKILL.md files use: `key: value` lines and simple lists."""
    if not text.startswith("---"):
        raise SkillError("SKILL.md must start with a --- front matter block")
    end = text.find("\n---", 3)
    if end < 0:
        raise SkillError("SKILL.md front matter is not closed with ---")
    meta: dict[str, Any] = {}
    key = None
    for raw in text[3:end].splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(("  ", "\t")) and key is not None:  # continuation or list item
            item = line.strip()
            if item.startswith("- "):
                meta[key] = (meta[key] if isinstance(meta.get(key), list) else []) + [item[2:].strip().strip("'\"")]
            else:
                meta[key] = (str(meta.get(key) or "") + " " + item).strip()
            continue
        k, sep, v = line.partition(":")
        if not sep:
            raise SkillError(f"front matter line is not 'key: value': {line!r}")
        key = k.strip()
        value = v.strip()
        meta[key] = value.strip("'\"") if value not in ("|", ">", "") else ""
    return meta, text[end + 4:].lstrip("\n")


def load_skill_dir(path: Path) -> Skill:
    md = path / "SKILL.md"
    if not md.exists():
        raise SkillError(f"{path} has no SKILL.md")
    meta, body = parse_frontmatter(md.read_text(encoding="utf-8"))
    name = str(meta.get("name") or "")
    description = " ".join(str(meta.get("description") or "").split())
    if not NAME_RE.match(name):
        raise SkillError(f"{md}: name must be lowercase letters, digits and dashes (got {name!r})")
    if name != path.name:
        raise SkillError(f"{md}: name {name!r} must match its folder name {path.name!r}")
    if not description:
        raise SkillError(f"{md}: a description is required (it is how the agent decides to use the skill)")
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(f"{md}: description is longer than {MAX_DESCRIPTION} characters")
    return Skill(name, description, path.resolve(), body[:MAX_BODY], meta)


class SkillLibrary:
    """Skills found in one or more directories (the first directory wins on a name clash)."""

    def __init__(self, dirs: Iterable[str | Path]) -> None:
        self.skills: dict[str, Skill] = {}
        self.errors: dict[str, str] = {}
        for d in dirs:
            root = Path(d).expanduser()
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                try:
                    skill = load_skill_dir(child)
                except SkillError as exc:
                    self.errors[child.name] = str(exc)
                    continue
                self.skills.setdefault(skill.name, skill)

    def names(self) -> list[str]:
        return sorted(self.skills)

    def snapshot(self, wanted: Iterable[str]) -> dict[str, Any]:
        """The skills a session may load ("*" = all), as recorded in its log."""
        wanted = list(wanted)
        names = self.names() if "*" in wanted else wanted
        found = [self.skills[n].summary() for n in names if n in self.skills]
        missing = {n: self.errors.get(n, "not installed") for n in names if n not in self.skills}
        return {"kind": "skills", "skills": found, "errors": missing}

    def install(self, name: str, workspace: Path) -> Path:
        """Copy a skill into the workspace so its files and scripts are reachable inside the sandbox."""
        skill = self.skills[name]
        target = workspace / SKILLS_DIR / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill.path, target, ignore=shutil.ignore_patterns(".*", "__pycache__"))
        return target


def render_block(skills: list[Mapping[str, Any]]) -> str:
    if not skills:
        return ""
    lines = "\n".join(f"- {s['name']}: {s['description']}" for s in skills)
    return ("\n\nSkills (instructions for particular kinds of work). When a task matches a skill's description, "
            f"call load_skill with its name before you start, then follow it:\n{lines}")
