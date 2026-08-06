"""Pydantic result models shared across probes, scoring, and the run bundle."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field


class Domain(StrEnum):
    """Probe domains (build plan §2). PLATFORM is the unscored harness self-check."""

    D2 = "D2"
    D4 = "D4"
    D6 = "D6"
    D8 = "D8"
    PLATFORM = "platform"


class Verdict(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


class Assurance(StrEnum):
    """Supply Assurance Level (§7): A=white-box, B=stable black-box, C=unverified, Disqualified."""

    A = "A"
    B = "B"
    C = "C"
    DISQUALIFIED = "Disqualified"


class TimingSample(BaseModel):
    kind: str  # ttft | tpot | itl | e2e
    ms: float


class ProbeResult(BaseModel):
    """One probe's verdict plus evidence refs. Verdicts are never bare (§10)."""

    probe_id: str
    domain: Domain
    verdict: Verdict
    score: float = 0.0
    weight: float = 1.0
    successes: int = 0
    attempts: int = 0
    notes: list[str] = Field(default_factory=list)
    evidence_ref: list[str] = Field(default_factory=list)
    curl: str | None = None
    samples: list[TimingSample] = Field(default_factory=list)
    error: str | None = None


class SurfaceMap(BaseModel):
    """API surface discovered once by P0; drives all §2 skip rules."""

    models: list[str] = Field(default_factory=list)
    claimed_present: bool = False
    responses_api: bool = False
    messages_api: bool = False
    logprobs: bool = False


class DomainScore(BaseModel):
    domain: Domain
    score: float
    probes: list[str] = Field(default_factory=list)
    verdict_counts: dict[str, int] = Field(default_factory=dict)


class SLA(BaseModel):
    """Client SLA thresholds for goodput (D2); global defaults, client overrides."""

    ttft_s: float = 5.0
    tpot_ms: float = 500.0
    e2e_s: float = 60.0


class Veto(BaseModel):
    """Independent disqualification: reverse identity, substitution, billing inflation, hidden origin."""

    code: str
    detail: str


class AssuranceVerdict(BaseModel):
    level: Assurance = Assurance.C
    basis: list[str] = Field(default_factory=list)
    vetoes: list[Veto] = Field(default_factory=list)


class CalibrationSnapshot(BaseModel):
    """Harness calibration snapshot (§13): P0 self-check + discovered surface.

    Captured once per run after P0 completes; drives assurance confidence.
    """

    p0_verdicts: dict[str, str] = Field(default_factory=dict)
    models_catalog: int = 0
    claimed_present: bool = False
    responses_api: bool = False
    messages_api: bool = False
    captured_at: str = ""


class RunBundle(BaseModel):
    """Top-level JSON output contract (§13)."""

    run_id: str
    endpoint: str
    claimed_models: list[str]
    mode: str
    started_at: str
    finished_at: str | None = None
    versions: dict[str, str] = Field(default_factory=dict)
    sla: SLA = Field(default_factory=SLA)
    overall_score: float | None = None
    domain_scores: dict[str, DomainScore] = Field(default_factory=dict)
    assurance: AssuranceVerdict = Field(default_factory=AssuranceVerdict)
    vetoes: list[Veto] = Field(default_factory=list)
    calibration: CalibrationSnapshot | None = None
    probes: list[ProbeResult] = Field(default_factory=list)
    transit: dict[str, Any] = Field(default_factory=dict)


class BudgetTracker(BaseModel):
    """Per-run cost cap (§12). Token math is naive until tokenizers land (M2)."""

    PRICE_PER_1K: ClassVar[float] = 0.005  # naive blended default, replaced by pricing table in M2

    budget_usd: float | None = None
    estimated_usd: float = 0.0
    requests: int = 0
    prompt_chars: int = 0
    completion_chars: int = 0
    blocked: bool = False

    def add(self, prompt_text: str, completion_text: str = "") -> None:
        self.requests += 1
        self.prompt_chars += len(prompt_text)
        self.completion_chars += len(completion_text)
        tokens = (self.prompt_chars + self.completion_chars) / 4
        self.estimated_usd = tokens / 1000 * self.PRICE_PER_1K
        if self.budget_usd is not None and self.estimated_usd >= self.budget_usd:
            self.blocked = True

    def remaining(self) -> float:
        if self.budget_usd is None:
            return float("inf")
        return max(0.0, self.budget_usd - self.estimated_usd)
