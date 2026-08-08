"""Versioned baseline records, filesystem store, and matching.

Contract: `docs/08-output-data-contract.md` §10 (file format, bundle
reference, matching rules) and `docs/06-m2-probe-spec.md` §2. A baseline
is a standalone JSON file ``baselines/<baseline_id>.json``; the JSON file
is the single source of truth. The SQLite ``baselines`` table
(docs/08 §11) is an M2 store-migration target and is intentionally not
created here.

Rules honoured:

- writers emit schema 2; readers accept schema 1 (missing fields get
  defaults) and reject schema > 2 with an explicit error (docs/08 §12).
- ``baseline_id`` matches ``BL-<LABEL>-<MODEL-OR-FAMILY>-<4-digit-seq>``
  and filenames are derived deterministically from it (safe path handling).
- files are immutable after writing (docs/08 MR4): a re-record creates the
  next sequence number via :meth:`BaselineStore.next_id`, never an overwrite.
- matching prefers exact version-pinned records; family/coarse tiers are
  opt-in; the newest matching record (highest ``captured_at``) wins.
- malformed or schema-newer files are rejected with explicit errors and
  never enter selection.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from supgate.evidence import redact_payload

BASELINE_SCHEMA_VERSION = 2


class BaselineError(Exception):
    """Base class for baseline store/selection failures."""


class BaselinePathError(BaselineError):
    """Unsafe baseline id / file path (traversal, separator, NUL, ...)."""


class MalformedBaselineError(BaselineError):
    """File exists but is not a valid baseline record."""


class UnsupportedBaselineError(BaselineError):
    """File schema is newer than this reader supports."""


class BaselineExistsError(BaselineError):
    """Save refused: baselines are immutable, a re-record gets a new seq."""


class BaselineRecordingError(BaselineError):
    """Baseline capture failed (p0 gate, transport, non-contract response)."""


class BaselineSurface(BaseModel):
    """API surface snapshot at baseline time (docs/08 §10.1)."""

    models_catalog: int = 0
    responses_api: bool = False
    messages_api: bool = False
    claimed_present: bool = False
    logprobs: bool = False


class IdPrefixFingerprint(BaseModel):
    family: str = ""
    samples: int = 0
    consistent: bool = False


class ModelEchoFingerprint(BaseModel):
    model: str = ""
    samples: int = 0
    consistent: bool = False


class ObjectFingerprint(BaseModel):
    object: str = ""
    samples: int = 0


class TimingFingerprint(BaseModel):
    """Streaming shape/timing fingerprint: median/p90 arrival statistics."""

    median: float | None = None
    p90: float | None = None
    n: int = 0


class MeanStdFingerprint(BaseModel):
    """Billing fingerprint shape (recount_deviation_pct, wrap_offset_tokens)."""

    mean: float | None = None
    std: float | None = None
    n: int = 0


class UsageSchemaFingerprint(BaseModel):
    cached_tokens: bool = False
    reasoning_tokens: bool = False


class BaselineRecord(BaseModel):
    """A versioned baseline record (schema v2; readers accept v1).

    Additive fields over docs/08 §10.1 — ``vendor`` / ``model`` /
    ``model_version`` / ``endpoint`` — make the pinned identity explicit so
    selection can distinguish exact version-pinned matches from optional
    family/coarse matches. ``fingerprints`` carries per-signal reference
    statistics (built with the typed helpers above) and leaves room for the
    D4 signal values recorded by later milestones.
    """

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    schema_version: int = Field(default=BASELINE_SCHEMA_VERSION, alias="schema")
    baseline_id: str
    provider_label: str = ""
    vendor: str = ""
    model: str = ""
    model_version: str | None = None
    endpoint: str = ""
    captured_at: str = ""
    claimed_models: list[str] = Field(default_factory=list)
    surface: BaselineSurface = Field(default_factory=BaselineSurface)
    fingerprints: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _schema_must_be_int(cls, value: Any) -> int:
        if isinstance(value, bool) or value is None:
            raise ValueError("schema must be an integer")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("schema must be an integer") from exc

    @property
    def schema(self) -> int:
        return self.schema_version

    @model_validator(mode="after")
    def _normalize_v1_defaults(self) -> BaselineRecord:
        if not self.model and self.claimed_models:
            self.model = self.claimed_models[0]
        if not self.vendor and self.provider_label:
            self.vendor = self.provider_label
        return self


def parse_baseline(doc: Any, *, filename: str | None = None) -> BaselineRecord:
    """Parse and validate one baseline document; rejects malformed/incompatible.

    Raises :class:`MalformedBaselineError` or :class:`UnsupportedBaselineError`
    with a message naming the problem. Missing ``schema`` is treated as 1
    (docs/08 §12.2: readers accept older schemas by defaulting missing fields).
    """

    if not isinstance(doc, dict):
        raise MalformedBaselineError("baseline file must be a JSON object")
    if "schema" not in doc:
        doc = {**doc, "schema": 1}
    raw_schema = doc.get("schema")
    if isinstance(raw_schema, bool):
        raise MalformedBaselineError("schema must be an integer")
    try:
        schema = int(raw_schema)
    except (TypeError, ValueError):
        raise MalformedBaselineError(f"schema {raw_schema!r} is not an integer") from None
    if schema > BASELINE_SCHEMA_VERSION:
        raise UnsupportedBaselineError(
            f"schema {schema} is newer than supported version {BASELINE_SCHEMA_VERSION}"
        )
    if schema < 1:
        raise MalformedBaselineError(f"invalid schema {schema}")
    try:
        record = BaselineRecord.model_validate(doc)
    except ValidationError as exc:
        detail = _first_error(exc)
        raise MalformedBaselineError(f"invalid baseline fields: {detail}") from exc
    if not record.baseline_id:
        raise MalformedBaselineError("missing baseline_id")
    if filename is not None and filename != f"{record.baseline_id}.json":
        raise MalformedBaselineError(
            f"baseline_id {record.baseline_id!r} does not match file name {filename!r}"
        )
    return record


def _first_error(exc: ValidationError) -> str:
    for error in exc.errors():
        where = ".".join(str(part) for part in error.get("loc", ()))
        message = error.get("msg", "invalid")
        return f"{where}: {message}"
    return "invalid record"


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text.strip()).strip("-").upper()
    return slug or "UNKNOWN"


_ID_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class BaselineStore:
    """Filesystem baseline store: deterministic safe naming, scan, get, save.

    Layout: ``<root>/<baseline_id>.json``. Only top-level ``*.json`` files
    are considered records; subdirectories (e.g. evidence) are ignored.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else Path("baselines")

    def path_for(self, baseline_id: str) -> Path:
        """Safe join of ``baseline_id`` into the store root.

        Rejects traversal, separators, NUL, and anything outside the
        ``[A-Za-z0-9._-]`` id alphabet so a crafted id can never escape the
        store directory.
        """

        if not baseline_id or "\x00" in baseline_id:
            raise BaselinePathError(f"invalid baseline id {baseline_id!r}")
        if (
            "/" in baseline_id
            or "\\" in baseline_id
            or baseline_id in {".", ".."}
            or not _ID_SAFE.fullmatch(baseline_id)
        ):
            raise BaselinePathError(f"unsafe baseline id {baseline_id!r}")
        root = self.root.resolve()
        path = (root / f"{baseline_id}.json").resolve()
        if not path.is_relative_to(root):
            raise BaselinePathError(f"baseline path escapes the store root: {baseline_id!r}")
        return path

    def next_id(self, *, label: str, model: str) -> str:
        """Deterministic next ``BL-<LABEL>-<MODEL>-<seq:04d>`` id.

        The sequence is derived from existing files only (no randomness, no
        overwrites — docs/08 MR4), starting at 0001.
        """

        prefix = f"BL-{_slug(label)}-{_slug(model)}-"
        existing: list[int] = []
        for path in self.root.glob("*.json"):
            stem = path.stem
            if stem.startswith(prefix):
                seq = stem[len(prefix) :]
                if seq.isdigit():
                    existing.append(int(seq))
        return f"{prefix}{max(existing, default=0) + 1:04d}"

    def scan(self) -> tuple[list[BaselineRecord], list[str]]:
        """Load every record; returns (records, errors).

        Malformed or schema-newer files never crash the listing: each is
        reported as ``"<path>: <reason>"`` so callers can surface it without
        letting it enter selection.
        """

        if not self.root.exists():
            return [], []
        records: list[BaselineRecord] = []
        errors: list[str] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                records.append(self._load_file(path))
            except (MalformedBaselineError, UnsupportedBaselineError) as exc:
                errors.append(f"{path.name}: {exc}")
        return records, errors

    def list(self) -> list[BaselineRecord]:
        return self.scan()[0]

    def get(self, baseline_id: str) -> BaselineRecord | None:
        """Load one record; None when absent; raises on malformed/incompatible."""

        path = self.path_for(baseline_id)
        if not path.exists():
            return None
        return self._load_file(path)

    def save(self, record: BaselineRecord, *, overwrite: bool = False) -> Path:
        """Persist one record (redacted) at ``<root>/<baseline_id>.json``.

        Refuses to overwrite an existing file unless ``overwrite`` is set —
        re-records must create a new sequence number (docs/08 MR4). All
        string values pass through the evidence redaction choke point so no
        key-shaped material can reach disk.
        """

        path = self.path_for(record.baseline_id)
        if path.exists() and not overwrite:
            raise BaselineExistsError(
                f"{record.baseline_id} already exists; baselines are immutable, "
                "a re-record creates the next sequence number"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = redact_payload(record.model_dump(mode="json"))
        path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
        return path

    def _load_file(self, path: Path) -> BaselineRecord:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MalformedBaselineError(f"not valid JSON: {exc}") from exc
        return parse_baseline(doc, filename=path.name)


class MatchKind(StrEnum):
    """Tier of a baseline match: exact version-pinned vs opt-in looser tiers."""

    EXACT = "exact"
    FAMILY = "family"
    COARSE = "coarse"


@dataclass(frozen=True)
class BaselineMatch:
    """A selected baseline plus the tier it matched on (docs/08 §10.2)."""

    kind: MatchKind
    record: BaselineRecord
    matched_on: list[str]


def _record_model(record: BaselineRecord) -> str:
    return record.model or (record.claimed_models[0] if record.claimed_models else "")


def _vendor_ok(record: BaselineRecord, vendor: str) -> bool:
    candidates = {c for c in (record.vendor, record.provider_label) if c}
    return any(c.lower() == vendor.lower() for c in candidates)


def _family_prefix(claimed: str, pinned: str) -> bool:
    """One model is the versioned family of the other (``gpt-4o`` / ``gpt-4o-*``)."""

    if not claimed or not pinned or claimed == pinned:
        return False
    return claimed.startswith(pinned + "-") or pinned.startswith(claimed + "-")


def _coarse_token(claimed: str, pinned: str) -> bool:
    if not claimed or not pinned:
        return False
    return claimed.split("-")[0] == pinned.split("-")[0]


def _matched_on(record: BaselineRecord, claimed_models: list[str], kind: MatchKind) -> list[str]:
    pinned = _record_model(record)
    if kind is MatchKind.EXACT:
        return [
            c for c in claimed_models if c == record.model or c in record.claimed_models
        ]
    if kind is MatchKind.FAMILY:
        return [c for c in claimed_models if _family_prefix(c, pinned)]
    return [c for c in claimed_models if _coarse_token(c, pinned)]


def _sort_key(record: BaselineRecord) -> tuple[str, str]:
    return (record.captured_at or "", record.baseline_id)


def select_baseline(
    records: list[BaselineRecord],
    claimed_models: list[str],
    *,
    vendor: str | None = None,
    allow_family: bool = False,
    allow_coarse: bool = False,
) -> BaselineMatch | None:
    """Select the best baseline for the claimed models (docs/08 §10.2).

    Tiers, in priority order: exact version-pinned match (always enabled),
    family-prefix match and coarse vendor/token match (both opt-in). Within
    the highest non-empty tier the newest record by ``captured_at`` wins
    (ISO-8601 UTC strings compare correctly; ties break by id). Returns
    None when no allowed tier matches.
    """

    if not claimed_models:
        raise ValueError("claimed_models must not be empty")
    candidates = [r for r in records if vendor is None or _vendor_ok(r, vendor)]
    tiers: list[tuple[MatchKind, bool]] = [
        (MatchKind.EXACT, True),
        (MatchKind.FAMILY, allow_family),
        (MatchKind.COARSE, allow_coarse),
    ]
    for kind, enabled in tiers:
        if not enabled:
            continue
        hits = [(r, _matched_on(r, claimed_models, kind)) for r in candidates]
        hits = [(r, m) for r, m in hits if m]
        if hits:
            record, matched_on = max(hits, key=lambda pair: _sort_key(pair[0]))
            return BaselineMatch(kind=kind, record=record, matched_on=matched_on)
    return None


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile over floats; None for empty input."""

    ordered = [v for v in values if v is not None]
    if not ordered:
        return None
    ordered.sort()
    rank = max(0, min(len(ordered) - 1, math.ceil(pct / 100.0 * len(ordered)) - 1))
    return ordered[rank]
