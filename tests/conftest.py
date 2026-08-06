"""Shared fixtures: fake server, run context, orchestrator, manifest path."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from supgate.evidence import EvidenceWriter
from supgate.models import BudgetTracker, SurfaceMap
from supgate.orchestrator import Orchestrator
from supgate.probes.base import RunContext
from tests.fake_server import FakeOpenAI

MANIFEST = Path(__file__).resolve().parent.parent / "supgate" / "manifests" / "probes.yaml"


@pytest.fixture
def fake_server() -> FakeOpenAI:
    return FakeOpenAI()


@pytest.fixture
def transport(fake_server: FakeOpenAI) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=fake_server)


@pytest.fixture
def client(fake_server: FakeOpenAI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_server), timeout=10)


@pytest.fixture
def manifest() -> Path:
    return MANIFEST


@pytest.fixture
def ctx(client: httpx.AsyncClient, fake_server: FakeOpenAI, tmp_path: Path) -> RunContext:
    return RunContext(
        endpoint="https://fake.example/v1",
        api_key=fake_server.valid_key,
        model="gpt-4o",
        claimed_models=["gpt-4o"],
        surface=SurfaceMap(models=list(fake_server.models), claimed_present=True),
        client=client,
        evidence=EvidenceWriter(tmp_path / "evidence", "TEST-RUN"),
        budget=BudgetTracker(),
    )


@pytest.fixture
def orchestrator(transport: httpx.ASGITransport) -> Orchestrator:
    return Orchestrator(concurrency=10, transport=transport)
