"""Record model calls to a cassette and replay them offline.

Replay is keyed by the full request digest. If a replayed session renders even
one byte differently from the recording, the lookup misses and the replay
fails loudly, which makes cassettes a determinism test for the context engine
as well as a way to run tests without a model.
"""

from __future__ import annotations

import dataclasses
import json
from collections import defaultdict, deque
from pathlib import Path

from .base import ModelClient, ModelError, ModelRequest, ModelResponse


class RecordingModel:
    def __init__(self, inner: ModelClient, path: str | Path) -> None:
        self.inner = inner
        self.name = inner.name
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self.inner.complete(request)
        entry = {"digest": request.digest(), "response": dataclasses.asdict(response)}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


class ReplayModel:
    def __init__(self, path: str | Path, name: str = "replay") -> None:
        self.name = name
        self._by_digest: dict[str, deque[ModelResponse]] = defaultdict(deque)
        self.misses = 0
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    entry = json.loads(line)
                    self._by_digest[entry["digest"]].append(ModelResponse(**entry["response"]))

    async def complete(self, request: ModelRequest) -> ModelResponse:
        key = request.digest()
        queue = self._by_digest.get(key)
        if not queue:
            self.misses += 1
            raise ModelError(
                f"cassette miss for request {key[:12]}: the rendered request differs from the recording",
                retriable=False,
            )
        return queue.popleft()

    async def aclose(self) -> None:
        return None
