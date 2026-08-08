"""SQLite run history (build plan §13, M6 ops; docs/08 §11, §18).

Schema is versioned via ``PRAGMA user_version`` (docs/08 §18 MR3). Version 2
is fully additive over the M1 store: ``runs`` gains ``schema_version`` /
``baseline_id`` / ``calibration_json``, ``probe_results`` gains
``metrics_json`` / ``notes_json``, and the ``baselines`` / ``vetoes`` tables
are created. Existing databases migrate in place with ``ALTER TABLE ADD
COLUMN`` and ``CREATE TABLE IF NOT EXISTS`` — user data is never dropped or
recreated, and re-running the migration is a no-op.

``record_run`` writes rows with explicit column names and persists the
schema-2 metadata when the bundle carries it (compatible with current
RunBundle defaults while schema-2 fields land in models.py).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from supgate.evidence import redact_payload, redact_text
from supgate.models import RunBundle

if TYPE_CHECKING:
    from supgate.baselines import BaselineRecord

SCHEMA_VERSION = 2

_RUNS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "TEXT PRIMARY KEY"),
    ("endpoint", "TEXT"),
    ("model", "TEXT"),
    ("mode", "TEXT"),
    ("started_at", "TEXT"),
    ("overall", "REAL"),
    ("assurance", "TEXT"),
    ("bundle_path", "TEXT"),
    ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
    ("baseline_id", "TEXT"),
    ("calibration_json", "TEXT"),
)

_PROBE_RESULTS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "TEXT"),
    ("probe_id", "TEXT"),
    ("verdict", "TEXT"),
    ("score", "REAL"),
    ("attempts", "INTEGER"),
    ("successes", "INTEGER"),
    ("evidence_path", "TEXT"),
    ("metrics_json", "TEXT"),
    ("notes_json", "TEXT"),
)

_BASELINES_COLUMNS: tuple[tuple[str, str], ...] = (
    ("baseline_id", "TEXT PRIMARY KEY"),
    ("provider_label", "TEXT"),
    ("claimed_models", "TEXT"),
    ("captured_at", "TEXT"),
    ("fingerprints_json", "TEXT"),
    ("bundle_path", "TEXT"),
)

_VETOES_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "TEXT"),
    ("code", "TEXT"),
    ("detail", "TEXT"),
)

_HISTORY_COLUMNS = ("run_id", "endpoint", "model", "mode", "started_at", "overall", "assurance", "bundle_path")


class RunStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".supgate" / "history.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            _migrate(conn)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record_run(self, bundle: RunBundle, bundle_path: Path | None = None) -> None:
        bundle = RunBundle.model_validate(redact_payload(bundle.model_dump(mode="json")))
        schema_version = _bundle_schema_version(bundle)
        baseline_id = _bundle_baseline_id(bundle)
        calibration = bundle.calibration
        if calibration is None:
            calibration_json = None
        elif hasattr(calibration, "model_dump_json"):
            calibration_json = calibration.model_dump_json()
        else:
            calibration_json = json.dumps(calibration, default=str)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, endpoint, model, mode, started_at, overall, assurance,
                    bundle_path, schema_version, baseline_id, calibration_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle.run_id,
                    bundle.endpoint,
                    ",".join(bundle.claimed_models),
                    bundle.mode,
                    bundle.started_at,
                    bundle.overall_score,
                    bundle.assurance.level.value,
                    redact_text(str(bundle_path)) if bundle_path else None,
                    schema_version,
                    baseline_id,
                    calibration_json,
                ),
            )
            conn.executemany(
                """
                INSERT INTO probe_results (
                    run_id, probe_id, verdict, score, attempts, successes,
                    evidence_path, metrics_json, notes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        bundle.run_id,
                        p.probe_id,
                        p.verdict.value,
                        p.score,
                        p.attempts,
                        p.successes,
                        ",".join(p.evidence_ref),
                        json.dumps(p.metrics, default=str) if getattr(p, "metrics", None) else None,
                        json.dumps(p.notes, default=str) if p.notes else None,
                    )
                    for p in bundle.probes
                ],
            )
            conn.executemany(
                "INSERT INTO vetoes (run_id, code, detail) VALUES (?, ?, ?)",
                [(bundle.run_id, v.code, v.detail) for v in bundle.vetoes],
            )

    def record_baseline(self, record: BaselineRecord, bundle_path: Path | None = None) -> None:
        """Persist one baseline record into the ``baselines`` table (docs/08 §11).

        Baselines are immutable (docs/08 MR4): recording an existing
        ``baseline_id`` leaves the stored row untouched, mirroring the file
        store's no-overwrite rule.
        """

        safe = redact_payload(record.model_dump(mode="json"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO baselines (
                    baseline_id, provider_label, claimed_models, captured_at,
                    fingerprints_json, bundle_path
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    safe["baseline_id"],
                    safe["provider_label"],
                    json.dumps(safe["claimed_models"], default=str),
                    safe["captured_at"],
                    json.dumps(safe["fingerprints"], default=str),
                    redact_text(str(bundle_path)) if bundle_path else None,
                ),
            )

    def history(self, endpoint: str | None = None, limit: int = 10) -> list[dict]:
        query = f"SELECT {', '.join(_HISTORY_COLUMNS)} FROM runs"
        params: tuple = ()
        if endpoint:
            query += " WHERE endpoint = ?"
            params = (redact_text(endpoint),)
        query += " ORDER BY started_at DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, params + (limit,)).fetchall()
        return [dict(zip(_HISTORY_COLUMNS, row, strict=True)) for row in rows]


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring the store to :data:`SCHEMA_VERSION` (docs/08 §11, §18 MR3).

    Version 2 is additive: every table is created with the target shape via
    ``CREATE TABLE IF NOT EXISTS`` (no-op on pre-existing M1 tables), then
    any columns missing from a pre-v2 table are backfilled with ``ALTER TABLE
    ADD COLUMN``. Existing rows are preserved untouched; M1 runs default to
    ``schema_version = 1``.
    """

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    if version >= SCHEMA_VERSION:
        return
    for table, columns in (
        ("runs", _RUNS_COLUMNS),
        ("probe_results", _PROBE_RESULTS_COLUMNS),
        ("baselines", _BASELINES_COLUMNS),
        ("vetoes", _VETOES_COLUMNS),
    ):
        conn.execute(_create_ddl(table, columns))
    _add_column(conn, "runs", "schema_version", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "runs", "baseline_id", "TEXT")
    _add_column(conn, "runs", "calibration_json", "TEXT")
    _add_column(conn, "probe_results", "metrics_json", "TEXT")
    _add_column(conn, "probe_results", "notes_json", "TEXT")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _create_ddl(table: str, columns: tuple[tuple[str, str], ...]) -> str:
    return f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(f'{name} {dtype}' for name, dtype in columns)})"


def _add_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _bundle_schema_version(bundle: RunBundle) -> int:
    """Bundle schema version: ``bundle.schema`` (int), else ``versions['schema']``
    (string per docs/08 §3), else 1 for M1 bundles."""

    raw = getattr(bundle, "schema", None)
    if callable(raw):  # pydantic's legacy BaseModel.schema classmethod, not a field
        raw = None
    if raw is None:
        versions = getattr(bundle, "versions", {}) or {}
        raw = versions.get("schema") if isinstance(versions, dict) else None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


def _bundle_baseline_id(bundle: RunBundle) -> str | None:
    """``baseline.baseline_id`` from the future schema-2 bundle field, or None."""

    baseline = getattr(bundle, "baseline", None)
    if baseline is None:
        return None
    value = baseline.get("baseline_id") if isinstance(baseline, dict) else getattr(baseline, "baseline_id", None)
    return value if isinstance(value, str) and value else None
