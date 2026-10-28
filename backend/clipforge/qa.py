"""Machine-readable QA report returned by every pipeline step.

The UI renders a report as a checklist. A report passes only if every check passes; checks
with severity "warn" are shown but never block.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["error", "warn"]


class QACheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""
    severity: Severity = "error"
    # Episode time in seconds the check refers to, when there is one.
    at_seconds: float | None = None


class QAReport(BaseModel):
    step: str
    checks: list[QACheck] = Field(default_factory=list)

    def check(
        self,
        name: str,
        passed: bool,
        detail: str = "",
        *,
        severity: Severity = "error",
        at_seconds: float | None = None,
    ) -> bool:
        self.checks.append(
            QACheck(
                name=name, passed=passed, detail=detail, severity=severity, at_seconds=at_seconds
            )
        )
        return passed

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.severity == "error")

    @property
    def failures(self) -> list[QACheck]:
        return [c for c in self.checks if not c.passed and c.severity == "error"]

    def to_dict(self) -> dict:
        return {**self.model_dump(), "passed": self.passed}
