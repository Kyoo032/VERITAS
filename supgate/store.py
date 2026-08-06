"""SQLite run history (build plan §13, M6 ops; schema reserved now)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from supgate.models import RunBundle


class RunStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".supgate" / "history.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
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
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS probe_results (
                    run_id TEXT,
                    probe_id TEXT,
                    verdict TEXT,
                    score REAL,
                    attempts INTEGER,
                    successes INTEGER,
                    evidence_path TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record_run(self, bundle: RunBundle, bundle_path: Path | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bundle.run_id,
                    bundle.endpoint,
                    ",".join(bundle.claimed_models),
                    bundle.mode,
                    bundle.started_at,
                    bundle.overall_score,
                    bundle.assurance.level.value,
                    str(bundle_path) if bundle_path else None,
                ),
            )
            conn.executemany(
                "INSERT INTO probe_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        bundle.run_id,
                        p.probe_id,
                        p.verdict.value,
                        p.score,
                        p.attempts,
                        p.successes,
                        ",".join(p.evidence_ref),
                    )
                    for p in bundle.probes
                ],
            )

    def history(self, endpoint: str | None = None, limit: int = 10) -> list[dict]:
        query = "SELECT * FROM runs"
        params: tuple = ()
        if endpoint:
            query += " WHERE endpoint = ?"
            params = (endpoint,)
        query += " ORDER BY started_at DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, params + (limit,)).fetchall()
        columns = ["run_id", "endpoint", "model", "mode", "started_at", "overall", "assurance", "bundle_path"]
        return [dict(zip(columns, row, strict=True)) for row in rows]
