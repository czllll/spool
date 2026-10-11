"""Agent Skills: SKILL.md parsing, discovery, progressive loading, and running a skill's script."""

from pathlib import Path

import pytest

from spool import events as ev
from spool.models.scripted import ScriptedModel, call, calls, reply
from spool.replay import verify_render
from spool.runtime import Runtime
from spool.skills import SkillError, SkillLibrary, load_skill_dir, parse_frontmatter

from .conftest import cfg, run

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "skills"


def write_skill(root: Path, name: str, front: str, body: str = "Do the thing.\n", files: dict | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{front}\n---\n{body}")
    for rel, text in (files or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    return d


def test_front_matter():
    meta, body = parse_frontmatter("---\nname: a-b\ndescription: First line\n  continued here\ntags:\n  - x\n  - 'y'\n---\n\n# Body\n")
    assert meta == {"name": "a-b", "description": "First line continued here", "tags": ["x", "y"]}
    assert body == "# Body\n"
    for bad in ("name: x\n", "---\nname: x\n", "---\njust words\n---\n"):
        with pytest.raises(SkillError):
            parse_frontmatter(bad)


def test_validation_and_discovery(tmp_path):
    write_skill(tmp_path, "good-one", "name: good-one\ndescription: Use for good things.")
    write_skill(tmp_path, "mismatch", "name: other-name\ndescription: x")
    write_skill(tmp_path, "no-desc", "name: no-desc")
    write_skill(tmp_path, "Bad_Name", "name: Bad_Name\ndescription: x")
    library = SkillLibrary([tmp_path, tmp_path / "missing"])
    assert library.names() == ["good-one"]
    assert set(library.errors) == {"mismatch", "no-desc", "Bad_Name"}
    snap = library.snapshot(["good-one", "mismatch", "absent"])
    assert [s["name"] for s in snap["skills"]] == ["good-one"]
    assert "must match its folder name" in snap["errors"]["mismatch"] and snap["errors"]["absent"] == "not installed"
    assert [s["name"] for s in library.snapshot(["*"])["skills"]] == ["good-one"]


def test_example_skills_are_valid():
    library = SkillLibrary([EXAMPLES])
    assert library.names() == ["expense-report", "meeting-scheduler"] and not library.errors
    assert load_skill_dir(EXAMPLES / "meeting-scheduler").summary()["files"] == ["reference.md"]


def test_progressive_loading_and_running_a_skill_script(store, tmp_path, workspace):
    root = tmp_path / "skills"
    write_skill(root, "csv-total", "name: csv-total\ndescription: Totals a CSV column. Use for sums of amounts.",
                body="Run `python3 .skills/csv-total/scripts/total.py FILE` and report the number it prints.\n",
                files={"scripts/total.py": "import csv, sys\nprint(sum(float(r['amount']) for r in csv.DictReader(open(sys.argv[1]))))\n"})
    write_skill(root, "unused", "name: unused\ndescription: Something else entirely.", body="SECRET BODY\n")
    (workspace / "spend.csv").write_text("item,amount\na,1.5\nb,2.25\n")
    model = ScriptedModel([
        calls([("load_skill", {"name": "csv-total"}, "c1")]),
        call("bash", command="python3 .skills/csv-total/scripts/total.py spend.csv", call_id="c2"),
        calls([("load_skill", {"name": "nope"}, "c3")]),
        reply("The total is 3.75."),
    ])
    rt = Runtime(store, model, data_dir=tmp_path / "data", skills=SkillLibrary([root]))
    sid = rt.start("Add up spend.csv", workspace=workspace, config=cfg(skills=["*"], tools=["bash", "read_file"]))
    assert run(rt.run(sid)).final_status == "completed"

    system = model.requests[0].system
    assert "- csv-total: Totals a CSV column." in system and "SECRET BODY" not in system  # names only, up front
    assert "load_skill" in [t["name"] for t in model.requests[0].tools]
    done = {e.data["call_id"]: e.data for e in store.load(sid) if e.type == ev.TOOL_COMPLETED}
    assert done["c1"]["output"].startswith("# Skill: csv-total") and "scripts/total.py" in done["c1"]["output"]
    assert (workspace / ".skills" / "csv-total" / "scripts" / "total.py").exists()
    assert "3.75" in done["c2"]["output"]
    assert not done["c3"]["ok"] and "available: csv-total, unused" in done["c3"]["error"]
    assert verify_render(store.load(sid)).ok


def test_skills_without_a_library_are_reported(store, tmp_path, workspace):
    rt = Runtime(store, ScriptedModel([reply("ok")]), data_dir=tmp_path / "data")
    sid = rt.start("t", workspace=workspace, config=cfg(skills=["meeting-scheduler"], tools=["read_file"]))
    run(rt.run(sid))
    note = next(e.data for e in store.load(sid) if e.type == ev.NOTE and e.data.get("kind") == "skills")
    assert note["skills"] == [] and "meeting-scheduler" in note["errors"]
