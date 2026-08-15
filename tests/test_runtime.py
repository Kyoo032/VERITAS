"""Runtime dependency metadata degrades explicitly when unavailable."""

from importlib import metadata

from supgate.runtime import _distribution_version, runtime_versions


def test_runtime_versions_contains_core_distributions():
    versions = runtime_versions()
    assert set(("python", "supgate", "httpx", "pydantic", "PyYAML", "tiktoken", "typer")) <= set(versions)
    assert all(isinstance(value, str) and value for value in versions.values())


def test_missing_distribution_metadata_is_explicit(monkeypatch):
    def missing(_: str) -> str:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr("supgate.runtime.metadata.version", missing)
    assert _distribution_version("missing") == "unavailable (distribution metadata missing)"
