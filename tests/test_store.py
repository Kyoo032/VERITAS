"""Schema-2 SQLite store migration and metadata writes (docs/08 §11, §18 MR3)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from supgate.baselines import BaselineRecord
from supgate.models import (
    AssuranceVerdict,
    BaselineReference,
    CalibrationSnapshot,
    Domain,
    InvocationConfig,
    ProbeResult,
    RunBundle,
    Verdict,
    Veto,
)
from supgate.store import SCHEMA_VERSION, RunStore

_M1_RUNS_DDL = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    endpoint TEXT,
    model TEXT,
    mode TEXT,
    started_at TEXT,
    overall REAL,
    assurance TEXT,
    bundle_path TEXT
)
"""

_M1_PROBE_RESULTS_DDL = """
CREATE TABLE probe_results (
    run_id TEXT,
    probe_id TEXT,
    verdict TEXT,
    score REAL,
    attempts INTEGER,
    successes INTEGER,
    evidence_path TEXT
)
"""

_M1_ROW = (
    "SUP-20260806-00A1",
    "https://api.supplier.example/v1",
    "gpt-4o",
    "full",
    "2026-08-06T09:00:00+00:00",
    87.5,
    "C",
    "runs/SUP-20260806-00A1.json",
)

_M1_PROBE_ROW = (
    "SUP-20260806-00A1",
    "d6.chat.basic",
    "pass",
    100.0,
    3,
    3,
    "SUP-20260806-00A1/d6.chat.basic_001.json",
)


def _make_m1_db(path: Path) -> None:
    """Recreate the M1 store exactly as the released harness wrote it."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(_M1_RUNS_DDL)
        conn.execute(_M1_PROBE_RESULTS_DDL)
        conn.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", _M1_ROW)
        conn.execute("INSERT INTO probe_results VALUES (?, ?, ?, ?, ?, ?, ?)", _M1_PROBE_ROW)
        conn.commit()
    finally:
        conn.close()


_V2_RUNS_DDL = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    endpoint TEXT,
    model TEXT,
    mode TEXT,
    started_at TEXT,
    overall REAL,
    assurance TEXT,
    bundle_path TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    baseline_id TEXT,
    calibration_json TEXT
)
"""

_V2_BASELINES_DDL = """
CREATE TABLE baselines (
    baseline_id TEXT PRIMARY KEY,
    provider_label TEXT,
    claimed_models TEXT,
    captured_at TEXT,
    fingerprints_json TEXT,
    bundle_path TEXT
)
"""


def _make_v2_db(path: Path) -> None:
    """Recreate a schema-2 store (pre key-identity columns) with one row."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(_V2_RUNS_DDL)
        conn.execute(_M1_PROBE_RESULTS_DDL)
        conn.execute(_V2_BASELINES_DDL)
        conn.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*_M1_ROW, 1, None, None))
        conn.execute("INSERT INTO probe_results VALUES (?, ?, ?, ?, ?, ?, ?)", _M1_PROBE_ROW)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()


def _make_v3_db(path: Path) -> None:
    """Create a v3-shaped store with a preserved legacy row."""
    _make_v2_db(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN key_env TEXT")
        conn.execute("ALTER TABLE runs ADD COLUMN key_fingerprint TEXT")
        conn.execute("ALTER TABLE baselines ADD COLUMN key_env TEXT")
        conn.execute("ALTER TABLE baselines ADD COLUMN key_fingerprint TEXT")
        conn.execute("CREATE TABLE vetoes (run_id TEXT, code TEXT, detail TEXT)")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()


def _table_columns(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return [row[1] for row in rows]


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        (version,) = conn.execute("PRAGMA user_version").fetchone()
    finally:
        conn.close()
    return version


def _schema2_bundle() -> RunBundle:
    """A schema-2-shaped bundle built from existing models.

    ``bundle.baseline`` does not exist on RunBundle yet; it is attached the
    way the schema-2 models will declare it, so the store must persist it
    when the attribute appears (concurrent schema work).
    """
    bundle = RunBundle(
        run_id="SUP-20260807-00B2",
        endpoint="https://api.supplier.example/v1",
        claimed_models=["gpt-4o"],
        mode="full",
        started_at="2026-08-07T09:00:00+00:00",
        finished_at="2026-08-07T09:03:40+00:00",
        versions={"supgate": "0.2.0", "manifest": "2", "schema": "2"},
        baseline=BaselineReference(
            baseline_id="BL-OFFICIAL-OPENAI-GPT4O-0001",
            matched_on=["gpt-4o"],
            captured_at="2026-08-06T00:00:00+00:00",
        ),
        overall_score=87.3,
        assurance=AssuranceVerdict(
            level="Disqualified",
            vetoes=[Veto(code="billing_inflation", detail="recount deviation +21.4% over 3 samples")],
        ),
        vetoes=[Veto(code="billing_inflation", detail="recount deviation +21.4% over 3 samples")],
        calibration=CalibrationSnapshot(
            p0_verdicts={"p0.echo": "pass", "p0.models": "pass", "p0.error_contract": "pass"},
            models_catalog=200,
            claimed_present=True,
            responses_api=True,
            messages_api=True,
            captured_at="2026-08-07T09:00:01+00:00",
        ),
        probes=[
            ProbeResult(
                probe_id="d4.recount_deviation",
                domain=Domain.D4,
                verdict=Verdict.FAIL,
                score=0.0,
                weight=2.0,
                successes=0,
                attempts=3,
                notes=["mean recount deviation +21.4% over 3 samples is inconsistent"],
                evidence_ref=["SUP-20260807-00B2/d4.recount_deviation_001.json"],
                metrics={"deviation_pct": 21.4, "encoding": "o200k_base"},
            ),
            ProbeResult(
                probe_id="d6.chat.basic",
                domain=Domain.D6,
                verdict=Verdict.PASS,
                score=100.0,
                successes=3,
                attempts=3,
            ),
        ],
    )
    return bundle


# --- fresh database ----------------------------------------------------------


def test_fresh_db_creates_latest_schema(tmp_path: Path):
    path = tmp_path / "history.db"
    RunStore(path)
    assert _user_version(path) == SCHEMA_VERSION == 4
    assert _table_columns(path, "runs") == [
        "run_id",
        "endpoint",
        "model",
        "mode",
        "started_at",
        "overall",
        "assurance",
        "bundle_path",
        "schema_version",
        "baseline_id",
        "calibration_json",
        "key_env",
        "key_fingerprint",
        "finished_at",
        "invocation_json",
        "versions_json",
    ]
    assert _table_columns(path, "probe_results") == [
        "run_id",
        "probe_id",
        "verdict",
        "score",
        "attempts",
        "successes",
        "evidence_path",
        "metrics_json",
        "notes_json",
    ]
    assert _table_columns(path, "baselines") == [
        "baseline_id",
        "provider_label",
        "claimed_models",
        "captured_at",
        "fingerprints_json",
        "bundle_path",
        "key_env",
        "key_fingerprint",
    ]
    assert _table_columns(path, "vetoes") == ["run_id", "code", "detail"]


# --- M1 -> v2 migration ------------------------------------------------------


def test_m1_db_migrates_and_preserves_rows(tmp_path: Path):
    path = tmp_path / "history.db"
    _make_m1_db(path)
    assert _user_version(path) == 0
    assert _table_columns(path, "runs") == [
        "run_id",
        "endpoint",
        "model",
        "mode",
        "started_at",
        "overall",
        "assurance",
        "bundle_path",
    ]

    RunStore(path)

    assert _user_version(path) == 4
    assert _table_columns(path, "runs") == [
        "run_id",
        "endpoint",
        "model",
        "mode",
        "started_at",
        "overall",
        "assurance",
        "bundle_path",
        "schema_version",
        "baseline_id",
        "calibration_json",
        "key_env",
        "key_fingerprint",
        "finished_at",
        "invocation_json",
        "versions_json",
    ]
    assert _table_columns(path, "probe_results") == [
        "run_id",
        "probe_id",
        "verdict",
        "score",
        "attempts",
        "successes",
        "evidence_path",
        "metrics_json",
        "notes_json",
    ]
    conn = sqlite3.connect(path)
    try:
        run = conn.execute(
            """
            SELECT run_id, endpoint, model, mode, started_at, overall, assurance,
                   bundle_path, schema_version, baseline_id, calibration_json
            FROM runs
            """
        ).fetchone()
        probe = conn.execute(
            """
            SELECT run_id, probe_id, verdict, score, attempts, successes,
                   evidence_path, metrics_json, notes_json
            FROM probe_results
            """
        ).fetchone()
        key_identity = conn.execute("SELECT key_env, key_fingerprint FROM runs").fetchone()
    finally:
        conn.close()
    assert run == (*_M1_ROW, 1, None, None)  # M1 row preserved; schema defaults to 1
    assert probe == (*_M1_PROBE_ROW, None, None)  # probe row preserved; new columns NULL
    assert key_identity == (None, None)  # v3 key columns default NULL for legacy rows


def test_v2_db_migrates_to_v3_preserving_rows(tmp_path: Path):
    path = tmp_path / "history.db"
    _make_v2_db(path)
    assert _user_version(path) == 2
    store = RunStore(path)
    assert _user_version(path) == 4
    assert _table_columns(path, "baselines") == [
        "baseline_id",
        "provider_label",
        "claimed_models",
        "captured_at",
        "fingerprints_json",
        "bundle_path",
        "key_env",
        "key_fingerprint",
    ]
    conn = sqlite3.connect(path)
    try:
        run = conn.execute("SELECT run_id, key_env, key_fingerprint FROM runs").fetchone()
        probes = conn.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0]
    finally:
        conn.close()
    assert run == ("SUP-20260806-00A1", None, None)
    assert probes == 1
    assert store.history()[0]["run_id"] == "SUP-20260806-00A1"


def test_migration_is_idempotent(tmp_path: Path):
    path = tmp_path / "history.db"
    _make_m1_db(path)
    RunStore(path)
    store = RunStore(path)  # re-open after migration: no-op, no errors
    store._init_schema()  # explicit re-run of the migration path
    assert _user_version(path) == 4
    assert _table_columns(path, "runs").count("schema_version") == 1
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT run_id FROM runs").fetchall()
        probes = conn.execute("SELECT run_id, probe_id FROM probe_results").fetchall()
    finally:
        conn.close()
    assert rows == [("SUP-20260806-00A1",)]  # preserved, not duplicated
    assert probes == [("SUP-20260806-00A1", "d6.chat.basic")]


def test_history_keeps_projection_after_migration(tmp_path: Path):
    path = tmp_path / "history.db"
    _make_m1_db(path)
    store = RunStore(path)
    assert store.history() == [
        {
            "run_id": "SUP-20260806-00A1",
            "endpoint": "https://api.supplier.example/v1",
            "model": "gpt-4o",
            "mode": "full",
            "started_at": "2026-08-06T09:00:00+00:00",
            "overall": 87.5,
            "assurance": "C",
            "bundle_path": "runs/SUP-20260806-00A1.json",
        }
    ]
    assert store.history(endpoint="https://other.example/v1") == []


# --- schema-2 metadata writes ------------------------------------------------


def test_record_run_writes_schema_2_metadata(tmp_path: Path):
    store = RunStore(tmp_path / "history.db")
    bundle = _schema2_bundle()
    bundle_path = tmp_path / f"{bundle.run_id}.json"
    store.record_run(bundle, bundle_path)

    conn = sqlite3.connect(store.path)
    try:
        run = conn.execute(
            """
            SELECT run_id, bundle_path, schema_version, baseline_id, calibration_json
            FROM runs
            """
        ).fetchone()
        probes = conn.execute(
            "SELECT probe_id, metrics_json, notes_json FROM probe_results ORDER BY probe_id"
        ).fetchall()
        vetoes = conn.execute("SELECT run_id, code, detail FROM vetoes").fetchall()
    finally:
        conn.close()
    assert run[0] == bundle.run_id
    assert run[1] == str(bundle_path)
    assert run[2] == 2  # from versions["schema"] == "2"
    assert run[3] == "BL-OFFICIAL-OPENAI-GPT4O-0001"
    assert json.loads(run[4]) == json.loads(bundle.calibration.model_dump_json())
    assert probes[0][0] == "d4.recount_deviation"
    assert json.loads(probes[0][1]) == {"deviation_pct": 21.4, "encoding": "o200k_base"}
    assert json.loads(probes[0][2]) == ["mean recount deviation +21.4% over 3 samples is inconsistent"]
    assert probes[1] == ("d6.chat.basic", None, None)  # empty metrics/notes stay NULL
    assert vetoes == [(bundle.run_id, "billing_inflation", "recount deviation +21.4% over 3 samples")]


def test_record_run_compatible_with_current_bundle_defaults(tmp_path: Path):
    """Current orchestrator bundles (no schema-2 fields set) still record."""
    store = RunStore(tmp_path / "history.db")
    bundle = RunBundle(
        run_id="SUP-20260807-00C3",
        endpoint="https://fake.example/v1",
        claimed_models=["gpt-4o"],
        mode="adhoc",
        started_at="2026-08-07T10:00:00+00:00",
        overall_score=87.5,
        assurance=AssuranceVerdict(level="C"),
        probes=[
            ProbeResult(
                probe_id="p0.echo",
                domain=Domain.PLATFORM,
                verdict=Verdict.PASS,
                score=100.0,
                attempts=1,
                successes=1,
            )
        ],
    )
    store.record_run(bundle)
    conn = sqlite3.connect(store.path)
    try:
        run = conn.execute("SELECT run_id, schema_version, baseline_id, calibration_json FROM runs").fetchone()
        veto_count = conn.execute("SELECT COUNT(*) FROM vetoes").fetchone()[0]
    finally:
        conn.close()
    assert run == (bundle.run_id, 1, None, None)
    assert veto_count == 0
    assert store.history() == [
        {
            "run_id": bundle.run_id,
            "endpoint": "https://fake.example/v1",
            "model": "gpt-4o",
            "mode": "adhoc",
            "started_at": "2026-08-07T10:00:00+00:00",
            "overall": 87.5,
            "assurance": "C",
            "bundle_path": None,
        }
    ]


def test_record_baseline_persists_record(tmp_path: Path):
    store = RunStore(tmp_path / "history.db")
    record = BaselineRecord(
        baseline_id="BL-OFFICIAL-OPENAI-GPT4O-0001",
        provider_label="openai",
        claimed_models=["gpt-4o"],
        captured_at="2026-08-06T00:00:00+00:00",
        fingerprints={"id_prefix": {"family": "chatcmpl", "samples": 12, "consistent": True}},
    )
    bundle_path = tmp_path / "baselines.json"
    store.record_baseline(record, bundle_path)

    conn = sqlite3.connect(store.path)
    try:
        row = conn.execute(
            """
            SELECT baseline_id, provider_label, claimed_models, captured_at,
                   fingerprints_json, bundle_path
            FROM baselines
            """
        ).fetchone()
    finally:
        conn.close()
    assert row == (
        "BL-OFFICIAL-OPENAI-GPT4O-0001",
        "openai",
        json.dumps(["gpt-4o"]),
        "2026-08-06T00:00:00+00:00",
        json.dumps({"id_prefix": {"family": "chatcmpl", "samples": 12, "consistent": True}}),
        str(bundle_path),
    )


def test_record_baseline_never_overwrites(tmp_path: Path):
    """Baselines are immutable (docs/08 MR4): same id keeps its first row."""
    store = RunStore(tmp_path / "history.db")
    record = BaselineRecord(
        baseline_id="BL-OFFICIAL-OPENAI-GPT4O-0001",
        provider_label="openai",
        claimed_models=["gpt-4o"],
        captured_at="2026-08-06T00:00:00+00:00",
    )
    first_path = tmp_path / "first.json"
    store.record_baseline(record, first_path)
    store.record_baseline(record, tmp_path / "second.json")
    conn = sqlite3.connect(store.path)
    try:
        rows = conn.execute("SELECT baseline_id, bundle_path FROM baselines").fetchall()
    finally:
        conn.close()
    assert rows == [("BL-OFFICIAL-OPENAI-GPT4O-0001", str(first_path))]


def test_record_run_defensively_redacts_artifact_fields(tmp_path: Path):
    secret = "sk-store-secret-1234567890"
    bundle = RunBundle(
        run_id="SUP-20260807-00D4",
        endpoint=f"https://api.example/v1?api_key={secret}",
        claimed_models=["gpt-4o"],
        mode="full",
        started_at="2026-08-07T11:00:00+00:00",
        probes=[
            ProbeResult(
                probe_id="test.leak",
                domain=Domain.PLATFORM,
                verdict=Verdict.PASS,
                score=100.0,
                attempts=1,
                successes=1,
                notes=[f"echoed {secret}"],
                evidence_ref=[f"SUP-X/{secret}.json"],
                metrics={"raw": secret, "url": f"https://x.test?token={secret}"},
            )
        ],
        vetoes=[Veto(code="test", detail=f"observed {secret}")],
    )
    store = RunStore(tmp_path / "history.db")
    store.record_run(bundle, tmp_path / f"{secret}.json")
    conn = sqlite3.connect(store.path)
    try:
        rows = conn.execute("SELECT endpoint, bundle_path FROM runs").fetchall()
        probes = conn.execute(
            "SELECT evidence_path, metrics_json, notes_json FROM probe_results"
        ).fetchall()
        vetoes = conn.execute("SELECT detail FROM vetoes").fetchall()
    finally:
        conn.close()
    serialized = json.dumps({"runs": rows, "probes": probes, "vetoes": vetoes})
    assert secret not in serialized
    assert "$SUPGATE_KEY" in serialized


def test_record_run_persists_key_identity(tmp_path: Path):
    store = RunStore(tmp_path / "history.db")
    bundle = _schema2_bundle()
    bundle.key_env = "MY_OFFICIAL_KEY"
    bundle.key_fingerprint = "sha256:" + "cd" * 32
    store.record_run(bundle)
    conn = sqlite3.connect(store.path)
    try:
        row = conn.execute("SELECT key_env, key_fingerprint FROM runs").fetchone()
    finally:
        conn.close()
    assert row == ("MY_OFFICIAL_KEY", "sha256:" + "cd" * 32)


def test_record_baseline_persists_key_identity(tmp_path: Path):
    store = RunStore(tmp_path / "history.db")
    record = BaselineRecord(
        baseline_id="BL-OFFICIAL-OPENAI-GPT4O-0001",
        provider_label="openai",
        claimed_models=["gpt-4o"],
        captured_at="2026-08-06T00:00:00+00:00",
        key_env="MY_OFFICIAL_KEY",
        key_fingerprint="sha256:" + "ef" * 32,
    )
    store.record_baseline(record)
    conn = sqlite3.connect(store.path)
    try:
        row = conn.execute("SELECT key_env, key_fingerprint FROM baselines").fetchone()
    finally:
        conn.close()
    assert row == ("MY_OFFICIAL_KEY", "sha256:" + "ef" * 32)


def test_v3_db_migrates_to_latest_preserving_rows(tmp_path: Path):
    path = tmp_path / "history.db"
    _make_v3_db(path)
    store = RunStore(path)
    assert _user_version(path) == SCHEMA_VERSION == 4
    assert _table_columns(path, "runs")[-3:] == [
        "finished_at",
        "invocation_json",
        "versions_json",
    ]
    assert store.history()[0]["run_id"] == _M1_ROW[0]
    assert store.inspect_run(_M1_ROW[0])["invocation"] == {}


def test_record_run_persists_config_versions_and_deep_inspection(tmp_path: Path):
    store = RunStore(tmp_path / "history.db")
    bundle = _schema2_bundle()
    bundle.invocation = InvocationConfig(
        base_url=bundle.endpoint,
        key_env="SUPPLIER_KEY",
        models=["gpt-4o"],
        mode="full",
    )
    store.record_run(bundle, tmp_path / "bundle.json")

    conn = sqlite3.connect(store.path)
    try:
        finished_at, invocation_json, versions_json = conn.execute(
            "SELECT finished_at, invocation_json, versions_json FROM runs WHERE run_id = ?",
            (bundle.run_id,),
        ).fetchone()
    finally:
        conn.close()
    assert finished_at == bundle.finished_at
    assert json.loads(invocation_json) == bundle.invocation.model_dump(mode="json")
    assert json.loads(versions_json) == bundle.versions

    detail = store.inspect_run(bundle.run_id)

    assert detail is not None
    assert detail["run_id"] == bundle.run_id
    assert detail["finished_at"] == bundle.finished_at
    assert detail["invocation"]["key_env"] == "SUPPLIER_KEY"
    assert detail["versions"] == bundle.versions
    assert detail["calibration"]["models_catalog"] == 200
    assert [probe["probe_id"] for probe in detail["probes"]] == [
        "d4.recount_deviation",
        "d6.chat.basic",
    ]
    assert detail["probes"][0]["metrics"]["deviation_pct"] == 21.4
    assert detail["probes"][0]["notes"] == bundle.probes[0].notes
    assert detail["probes"][0]["evidence_ref"] == bundle.probes[0].evidence_ref
    assert detail["vetoes"] == [
        {"code": "billing_inflation", "detail": "recount deviation +21.4% over 3 samples"}
    ]
    assert store.inspect_run("SUP-MISSING") is None


def test_inspect_legacy_run_uses_safe_json_defaults(tmp_path: Path):
    path = tmp_path / "history.db"
    _make_m1_db(path)
    store = RunStore(path)
    detail = store.inspect_run(_M1_ROW[0])
    assert detail is not None
    assert detail["finished_at"] is None
    assert detail["invocation"] == {}
    assert detail["versions"] == {}
    assert detail["calibration"] == {}
    assert detail["probes"][0]["metrics"] == {}
    assert detail["probes"][0]["notes"] == []
    assert detail["probes"][0]["evidence_ref"] == [_M1_PROBE_ROW[6]]
    assert detail["vetoes"] == []
