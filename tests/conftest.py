from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from spool.config import AgentConfig
from spool.runtime import Runtime
from spool.store import EventStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    s = EventStore(tmp_path / "spool.db")
    yield s
    s.close()


@pytest.fixture
def make_runtime(tmp_path: Path, store: EventStore):
    created: list[Runtime] = []

    def factory(model, **kwargs) -> Runtime:
        kwargs.setdefault("data_dir", tmp_path / "data")
        rt = Runtime(store, model, **kwargs)
        created.append(rt)
        return rt

    yield factory
    for rt in created:
        asyncio.run(rt.aclose())


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def cfg(**overrides) -> AgentConfig:
    base = dict(model="scripted", sandbox="process", temperature=None)
    base.update(overrides)
    return AgentConfig(**base)
