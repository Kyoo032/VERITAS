"""Pydantic result models shared across probes, scoring, and the run bundle."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from supgate.tokenizers import (
    DEFAULT_MODEL,
    FALLBACK_ENCODING,
    FALLBACK_RATES,
    ModelRates,
    TokenizerService,
    resolve_rates,
)


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


class StreamedEvent(BaseModel):
    """One SSE ``data:`` payload as observed by :meth:`RunContext.stream` (§3.3).

    ``delta`` is the running reassembled content text at this event;
    ``arrived_ms`` is a ``time.perf_counter()`` monotonic arrival timestamp;
    ``usage`` carries the usage block when the chunk includes one
    (``stream_options.include_usage``).
    """

    delta: str
    arrived_ms: float
    usage: dict[str, Any] | None = None


class StreamResult(BaseModel):
    """Completed typed result of one streamed exchange (§3.3, §8).

    ``body`` is the raw buffered SSE text (also the evidence body); timing
    values are client-side wall ms (TTFT from request start, E2E total,
    ``inter_event_ms`` one delay per pair of consecutive events). Probes map
    these onto TimingSamples/metrics.
    """

    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    events: list[StreamedEvent] = Field(default_factory=list)
    ttft_ms: float | None = None
    e2e_ms: float = 0.0
    inter_event_ms: list[float] = Field(default_factory=list)
    curl: str | None = None
    evidence_ref: str | None = None
    attempts: int = 1


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
    metrics: dict[str, Any] = Field(default_factory=dict)
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


class BaselineReference(BaseModel):
    """Baseline selected for a run, including match provenance (docs/08 §10.2)."""

    baseline_id: str
    matched_on: list[str] = Field(default_factory=list)
    captured_at: str = ""


class Transit(BaseModel):
    """Conservative transit-path analysis (docs/08 §9)."""

    hop_lower_bound: int = 1
    origin_class: str = "unknown"
    hop_hints: list[str] = Field(default_factory=list)


class Authenticity(BaseModel):
    """Report-level synthesis of D4 identity and integrity evidence."""

    verdict: str = "inconclusive"
    confidence: float = 0.0
    signal_families: list[str] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _confidence_in_unit_interval(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be in [0, 1] (docs/08 §3)")
        return value


class CostSummary(BaseModel):
    """Schema-2 token/cost summary backed by real tokenizer counts."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    requests: int = 0
    estimated_usd: float = 0.0
    blocked: bool = False
    model: str | None = Field(default=None, exclude=True)


class RunBundle(BaseModel):
    """Top-level JSON output contract (§13; schema-2 fields per docs/08 §3)."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    run_id: str
    endpoint: str
    claimed_models: list[str]
    mode: str
    started_at: str
    finished_at: str | None = None
    schema_version: int | None = Field(default=None, alias="schema")
    versions: dict[str, str | int] = Field(default_factory=dict)
    sla: SLA = Field(default_factory=SLA)
    cost: CostSummary = Field(default_factory=CostSummary)
    overall_score: float | None = None
    domain_scores: dict[str, DomainScore] = Field(default_factory=dict)
    assurance: AssuranceVerdict = Field(default_factory=AssuranceVerdict)
    vetoes: list[Veto] = Field(default_factory=list)
    calibration: CalibrationSnapshot | None = None
    surface: SurfaceMap = Field(default_factory=SurfaceMap)
    baseline: BaselineReference | None = None
    probes: list[ProbeResult] = Field(default_factory=list)
    transit: Transit = Field(default_factory=Transit)
    inconclusive: bool = False
    inconclusive_reason: str | None = None
    authenticity: Authenticity = Field(default_factory=Authenticity)

    @property
    def schema(self) -> int | None:
        return self.schema_version


class BudgetTracker(BaseModel):
    """Per-run cost cap (§12) with real token accounting (S1).

    Counts prompt/completion tokens with the configured
    :class:`~supgate.tokenizers.TokenizerService` (tiktoken) and prices them
    with the per-model input/output rate table (USD per 1K tokens) from
    :mod:`supgate.tokenizers`. The M1 naive ``chars/4`` heuristic,
    ``PRICE_PER_1K``, ``prompt_chars``, and ``completion_chars`` are gone.

    ``model`` (or a per-``add`` override) selects the encoding and the rate
    row; when the model is unknown the tracker falls back to
    ``FALLBACK_ENCODING``/``FALLBACK_RATES`` so the cost guard keeps
    working. ``tokenizer`` and ``rates`` may be injected (e.g. test doubles
    or custom pricing).
    """

    budget_usd: float | None = None
    estimated_usd: float = 0.0
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    blocked: bool = False
    model: str | None = None
    tokenizer: TokenizerService = Field(default_factory=TokenizerService)
    rates: ModelRates | None = None

    def add(
        self,
        prompt_text: str,
        completion_text: str = "",
        *,
        model: str | None = None,
    ) -> None:
        """Account one request: real token counts priced at per-1K rates.

        ``model`` overrides the tracker-level model for this call only;
        callers that already pass ``(prompt_text, completion_text)`` keep
        working unchanged (the tracker-level ``model`` or ``DEFAULT_MODEL``
        applies).
        """

        model = model or self.model or DEFAULT_MODEL
        encoding = self.tokenizer.resolve_encoding(model) or FALLBACK_ENCODING
        prompt_tokens = self.tokenizer.count(prompt_text, encoding)
        completion_tokens = self.tokenizer.count(completion_text, encoding)
        rates = self.rates or resolve_rates(model) or FALLBACK_RATES
        self.requests += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.estimated_usd += (
            prompt_tokens * rates.input_per_1k + completion_tokens * rates.output_per_1k
        ) / 1000.0
        if self.budget_usd is not None and self.estimated_usd >= self.budget_usd:
            self.blocked = True

    def remaining(self) -> float:
        if self.budget_usd is None:
            return float("inf")
        return max(0.0, self.budget_usd - self.estimated_usd)

    def summary(self) -> CostSummary:
        """Schema-2 :class:`CostSummary` snapshot of the tracker state."""

        return CostSummary(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            requests=self.requests,
            estimated_usd=self.estimated_usd,
            blocked=self.blocked,
            model=self.model,
        )
