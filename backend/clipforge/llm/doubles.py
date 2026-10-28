"""TEST DOUBLES ONLY. Never used for real runs.

`FakeLLM` is selected only when FAKE_MODELS=true (pipeline plumbing tests and CI). Its answers
are canned and deterministic, it reports `real = False`, and every QA report built on its
output carries a failing "real ..." check so a stub result can never pass as a real one.
"""

from __future__ import annotations

import json
import re
from typing import Any

from clipforge.llm.base import Logger, ProviderStatus

_LINE = re.compile(r"^\[(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\] [^:]+: (.*)$")


class FakeLLM:
    name = "fake"
    model = "test-double"
    real = False
    context_tokens = None

    def check(self) -> ProviderStatus:
        return ProviderStatus(
            True, self.name, self.model, "FAKE_MODELS test double (not real inference)", real=False
        )

    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        log: Logger,
        *,
        max_output_tokens: int = 4096,
        images: list[bytes] | None = None,
    ) -> str:
        props = set(schema.get("properties", {}))
        if props == {"clips"}:
            return self._pick(user)
        handler = getattr(self, f"_answer_{schema.get('title', '')}", None)
        if handler is None:
            raise NotImplementedError(f"FakeLLM has no canned answer for schema {sorted(props)}")
        return json.dumps(handler(user))

    @staticmethod
    def _pick(user: str) -> str:
        n = int(re.search(r"N = (\d+)", user).group(1))
        lines = []
        for raw in user.splitlines():
            m = _LINE.match(raw.strip())
            if m:
                lines.append((float(m.group(1)), float(m.group(2)), m.group(3)))
        clips = []
        i = 0
        while i < len(lines) and len(clips) < n:
            start = lines[i][0]
            j = i
            while j + 1 < len(lines) and lines[j + 1][1] - start <= 24:
                j += 1
            if lines[j][1] - start >= 18:
                clips.append(
                    {
                        "segments": [
                            {
                                "start": start,
                                "end": lines[j][1],
                                "text": " ".join(x[2] for x in lines[i : j + 1]),
                            }
                        ],
                        "hook": lines[i][2],
                        "context": lines[min(i + 1, j)][2],
                        "payoff": lines[j][2],
                        "virality_score": max(1, 8 - len(clips)),
                        "reasons": ["stub reason 1", "stub reason 2", "stub reason 3"],
                        "title_options": ["Stub title A 🔥", '"Stub quote" 😮', "Stub C 🎯"],
                        "broll_beats": ["stub beat"],
                        "risk_flags": [],
                        "improvements": "stub",
                    }
                )
            i = j + 1
        return json.dumps({"clips": clips})
