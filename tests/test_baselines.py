"""Baseline store: round-trip, deterministic safe naming, malformed data,
v1 compatibility, and exact/family/coarse selection (docs/08 §10, docs/06 §2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supgate.baselines import (
    BASELINE_SCHEMA_VERSION,
    BaselineExistsError,
    BaselinePathError,
    BaselineRecord,
    BaselineStore,
    BaselineSurface,
    MalformedBaselineError,
    MatchKind,
    UnsupportedBaselineError,
    parse_baseline,
    select_baseline,
)


def _record(
    baseline_id: str = "BL-OPENAI-GPT-4O-0001",
    *,
    vendor: str = "openai",
    model: str = "gpt-4o",
    claimed: list[str] | None = None,
    captured_at: str = "2026-08-06T00:00:00+00:00",
    schema: int = BASELINE_SCHEMA_VERSION,
    **kwargs,
) -> BaselineRecord:
    fields = {
        "schema": schema,
        "baseline_id": baseline_id,
        "provider_label": vendor,
        "vendor": vendor,
        "model": model,
        "endpoint": "https://api.openai.com/v1",
        "captured_at": captured_at,
        "claimed_models": claimed or [model],
        "surface": BaselineSurface(models_catalog=200, claimed_present=True),
        "fingerprints": {
            "id_prefix": {"family": "chatcmpl-", "samples": 3, "consistent": True},
            "headers_stable_set": ["content-type", "server"],
        },
    }
    fields.update(kwargs)
    return BaselineRecord(**fields)


# --- round-trip ------------------------------------------------------------


def test_round_trip_save_get(tmp_path: Path):
    store = BaselineStore(tmp_path)
    record = _record()
    path = store.save(record)

    assert path == tmp_path / "BL-OPENAI-GPT-4O-0001.json"
    assert path.exists()
    assert store.get("BL-OPENAI-GPT-4O-0001").model_dump() == record.model_dump()


def test_scan_returns_records_in_deterministic_order(tmp_path: Path):
    store = BaselineStore(tmp_path)
    store.save(_record("BL-OPENAI-GPT-4O-0002", captured_at="2026-08-06T01:00:00+00:00"))
    store.save(_record("BL-OPENAI-GPT-4O-0001"))
    records, errors = store.scan()
    assert errors == []
    assert [r.baseline_id for r in records] == ["BL-OPENAI-GPT-4O-0001", "BL-OPENAI-GPT-4O-0002"]
    assert store.list() == records


def test_missing_root_scans_empty(tmp_path: Path):
    assert BaselineStore(tmp_path / "missing").scan() == ([], [])
    assert BaselineStore(tmp_path / "missing").get("BL-X-0001") is None


# --- deterministic safe naming ---------------------------------------------


def test_next_id_sequences_deterministically(tmp_path: Path):
    store = BaselineStore(tmp_path)
    assert store.next_id(label="openai", model="gpt-4o") == "BL-OPENAI-GPT-4O-0001"
    store.save(_record(store.next_id(label="openai", model="gpt-4o")))
    assert store.next_id(label="openai", model="gpt-4o") == "BL-OPENAI-GPT-4O-0002"
    store.save(_record(store.next_id(label="openai", model="gpt-4o")))
    assert store.next_id(label="openai", model="gpt-4o") == "BL-OPENAI-GPT-4O-0003"


def test_next_id_slugs_label_and_model(tmp_path: Path):
    store = BaselineStore(tmp_path)
    assert store.next_id(label="My Vendor!", model="gpt 4o/2024") == "BL-MY-VENDOR-GPT-4O-2024-0001"
    assert store.next_id(label="", model="gpt-4o") == "BL-UNKNOWN-GPT-4O-0001"


def test_save_refuses_overwrite(tmp_path: Path):
    store = BaselineStore(tmp_path)
    store.save(_record())
    with pytest.raises(BaselineExistsError):
        store.save(_record(captured_at="2026-08-07T00:00:00+00:00"))
    store.save(_record(captured_at="2026-08-07T00:00:00+00:00"), overwrite=True)


@pytest.mark.parametrize(
    "bad_id",
    ["../escape", "a/b", "a\\b", "..", ".", "a\x00b", "a b", "", "BL-ID!"],
)
def test_path_for_rejects_unsafe_ids(tmp_path: Path, bad_id: str):
    with pytest.raises(BaselinePathError):
        BaselineStore(tmp_path).path_for(bad_id)


def test_path_for_stays_inside_root(tmp_path: Path):
    store = BaselineStore(tmp_path)
    assert store.path_for("BL-OPENAI-GPT-4O-0001") == tmp_path / "BL-OPENAI-GPT-4O-0001.json"


def test_save_redacts_key_shaped_values(tmp_path: Path):
    record = _record(
        fingerprints={
            "id_prefix": {"family": "chatcmpl-", "samples": 3, "consistent": True},
            "leak": "sk-secret1234567890",
        },
        notes=["token sk-abcdef1234567890 echoed"],
        endpoint="https://api.openai.com/v1?api_key=sk-secret1234567890",
    )
    BaselineStore(tmp_path).save(record)
    text = (tmp_path / "BL-OPENAI-GPT-4O-0001.json").read_text(encoding="utf-8")
    assert "sk-secret1234567890" not in text
    assert "sk-abc****7890" in text
    loaded = BaselineStore(tmp_path).get("BL-OPENAI-GPT-4O-0001")
    assert "sk-secret1234567890" not in json.dumps(loaded.model_dump())


# --- malformed / incompatible data -----------------------------------------


def _write(tmp_path: Path, filename: str, doc) -> Path:
    path = tmp_path / filename
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_invalid_json_rejected_in_scan(tmp_path: Path):
    (tmp_path / "BL-OPENAI-GPT-4O-0001.json").write_text("{not json", encoding="utf-8")
    records, errors = BaselineStore(tmp_path).scan()
    assert records == []
    assert len(errors) == 1 and "not valid JSON" in errors[0]


def test_non_object_document_rejected(tmp_path: Path):
    _write(tmp_path, "BL-OPENAI-GPT-4O-0001.json", [1, 2, 3])
    records, errors = BaselineStore(tmp_path).scan()
    assert records == [] and "JSON object" in errors[0]


def test_missing_baseline_id_rejected(tmp_path: Path):
    _write(tmp_path, "BL-OPENAI-GPT-4O-0001.json", {"schema": 2, "vendor": "openai", "model": "gpt-4o"})
    records, errors = BaselineStore(tmp_path).scan()
    assert records == [] and "baseline_id" in errors[0]


def test_filename_id_mismatch_rejected(tmp_path: Path):
    _write(
        tmp_path, "BL-OPENAI-GPT-4O-0001.json",
        {"schema": 2, "baseline_id": "BL-OPENAI-GPT-4O-9999", "vendor": "openai", "model": "gpt-4o"},
    )
    records, errors = BaselineStore(tmp_path).scan()
    assert records == [] and "does not match" in errors[0]


def test_wrong_field_types_rejected(tmp_path: Path):
    doc = _record().model_dump()
    doc["surface"] = "not-an-object"
    _write(tmp_path, "BL-OPENAI-GPT-4O-0001.json", doc)
    records, errors = BaselineStore(tmp_path).scan()
    assert records == [] and "surface" in errors[0]


def test_schema_newer_than_supported_rejected(tmp_path: Path):
    _write(tmp_path, "BL-OPENAI-GPT-4O-0001.json", {**_record().model_dump(), "schema": 3})
    records, errors = BaselineStore(tmp_path).scan()
    assert records == []
    assert "newer than supported version 2" in errors[0]
    with pytest.raises(UnsupportedBaselineError):
        BaselineStore(tmp_path).get("BL-OPENAI-GPT-4O-0001")


def test_non_integer_schema_rejected(tmp_path: Path):
    _write(tmp_path, "BL-OPENAI-GPT-4O-0001.json", {**_record().model_dump(), "schema": "abc"})
    records, errors = BaselineStore(tmp_path).scan()
    assert records == [] and "not an integer" in errors[0]


def test_malformed_get_raises_absent_returns_none(tmp_path: Path):
    store = BaselineStore(tmp_path)
    _write(tmp_path, "BL-OPENAI-GPT-4O-0001.json", "{broken")
    with pytest.raises(MalformedBaselineError):
        store.get("BL-OPENAI-GPT-4O-0001")
    assert store.get("BL-NOT-THERE-0001") is None


# --- v1 compatibility -------------------------------------------------------


def test_v1_record_loads_with_defaults(tmp_path: Path):
    v1 = {
        "schema": 1,
        "baseline_id": "BL-OPENAI-GPT-4O-0001",
        "provider_label": "OpenAI",
        "claimed_models": ["gpt-4o"],
        "captured_at": "2026-08-01T00:00:00+00:00",
        "surface": {"models_catalog": 100, "claimed_present": True},
        "fingerprints": {"id_prefix": {"family": "chatcmpl-", "samples": 5, "consistent": True}},
        "notes": [],
    }
    _write(tmp_path, "BL-OPENAI-GPT-4O-0001.json", v1)
    record = BaselineStore(tmp_path).get("BL-OPENAI-GPT-4O-0001")
    assert record.schema == 1
    assert record.model == "gpt-4o"  # normalized from claimed_models
    assert record.vendor == "OpenAI"  # normalized from provider_label
    assert record.endpoint == ""
    assert record.surface.models_catalog == 100


def test_missing_schema_treated_as_v1(tmp_path: Path):
    doc = _record().model_dump()
    del doc["schema"]
    _write(tmp_path, "BL-OPENAI-GPT-4O-0001.json", doc)
    assert BaselineStore(tmp_path).get("BL-OPENAI-GPT-4O-0001").schema == 1


def test_unknown_fields_ignored(tmp_path: Path):
    doc = _record().model_dump()
    doc["future_field"] = {"anything": 1}
    _write(tmp_path, "BL-OPENAI-GPT-4O-0001.json", doc)
    assert BaselineStore(tmp_path).get("BL-OPENAI-GPT-4O-0001").baseline_id == "BL-OPENAI-GPT-4O-0001"


def test_parse_baseline_accepts_typed_dump(tmp_path: Path):
    record = _record()
    parsed = parse_baseline(json.loads(record.model_dump_json()), filename="BL-OPENAI-GPT-4O-0001.json")
    assert parsed == record


# --- selection: exact vs family vs coarse ----------------------------------


def test_select_prefers_exact_over_family():
    exact = _record("BL-OPENAI-GPT-4O-0001", model="gpt-4o")
    family = _record("BL-OPENAI-GPT-4O-2024-0001", model="gpt-4o-2024-08-06", captured_at="2026-08-07T00:00:00+00:00")
    match = select_baseline([family, exact], ["gpt-4o"], allow_family=True)
    assert match.kind is MatchKind.EXACT
    assert match.record.baseline_id == "BL-OPENAI-GPT-4O-0001"
    assert match.matched_on == ["gpt-4o"]


def test_select_exact_is_version_pinned():
    pinned = _record("BL-OPENAI-GPT-4O-2024-0001", model="gpt-4o-2024-08-06")
    match = select_baseline([pinned], ["gpt-4o-2024-08-06"])
    assert match.kind is MatchKind.EXACT


def test_family_match_is_opt_in():
    family = _record("BL-OPENAI-GPT-4O-0001", model="gpt-4o")
    assert select_baseline([family], ["gpt-4o-2024-08-06"]) is None
    match = select_baseline([family], ["gpt-4o-2024-08-06"], allow_family=True)
    assert match.kind is MatchKind.FAMILY
    assert match.matched_on == ["gpt-4o-2024-08-06"]


def test_family_match_reverse_direction():
    pinned = _record("BL-OPENAI-GPT-4O-2024-0001", model="gpt-4o-2024-08-06")
    match = select_baseline([pinned], ["gpt-4o"], allow_family=True)
    assert match.kind is MatchKind.FAMILY
    assert match.record.baseline_id == "BL-OPENAI-GPT-4O-2024-0001"


def test_family_does_not_fire_for_unrelated_models():
    record = _record("BL-OPENAI-GPT-4O-0001", model="gpt-4o")
    assert select_baseline([record], ["claude-3-5-sonnet-20241022"], allow_family=True) is None


def test_newest_matching_baseline_wins():
    older = _record("BL-OPENAI-GPT-4O-0001", model="gpt-4o", captured_at="2026-08-06T00:00:00+00:00")
    newer = _record("BL-OPENAI-GPT-4O-0002", model="gpt-4o", captured_at="2026-08-07T00:00:00+00:00")
    match = select_baseline([older, newer], ["gpt-4o"])
    assert match.record.baseline_id == "BL-OPENAI-GPT-4O-0002"


def test_vendor_filter_restricts_matches():
    openai_rec = _record("BL-OPENAI-GPT-4O-0001", vendor="openai", model="gpt-4o")
    anthropic_rec = _record("BL-ANTHROPIC-GPT4O-0001", vendor="anthropic", model="gpt-4o")
    assert select_baseline([openai_rec, anthropic_rec], ["gpt-4o"], vendor="openai").record == openai_rec
    assert select_baseline([openai_rec], ["gpt-4o"], vendor="anthropic") is None


def test_coarse_match_is_opt_in_and_last():
    coarse = _record("BL-OPENAI-GPT35-0001", model="gpt-3.5-turbo")
    assert select_baseline([coarse], ["gpt-4o"]) is None
    assert select_baseline([coarse], ["claude-3-5-sonnet-20241022"], allow_coarse=True) is None
    match = select_baseline([coarse], ["gpt-4o"], allow_coarse=True)
    assert match is not None
    assert match.kind is MatchKind.COARSE


def test_coarse_never_beats_family_or_exact():
    exact = _record("BL-OPENAI-GPT-4O-0001", model="gpt-4o", captured_at="2026-08-01T00:00:00+00:00")
    coarse = _record("BL-OPENAI-GPT-4O-MINI-0001", model="gpt-4o-mini", captured_at="2026-08-07T00:00:00+00:00")
    match = select_baseline([coarse, exact], ["gpt-4o"], allow_coarse=True, allow_family=True)
    assert match.kind is MatchKind.EXACT


def test_no_match_returns_none():
    record = _record(model="gpt-4o")
    assert select_baseline([record], ["claude-3-5-sonnet-20241022"]) is None
    assert select_baseline([], ["gpt-4o"]) is None


def test_select_rejects_empty_claimed_models():
    with pytest.raises(ValueError):
        select_baseline([], [])


def test_matched_on_reports_which_claimed_models_hit():
    record = _record("BL-OPENAI-GPT-4O-0001", model="gpt-4o", claimed=["gpt-4o"])
    match = select_baseline([record], ["gpt-4o", "gpt-4o-mini"], allow_family=True)
    assert match.kind is MatchKind.EXACT
    assert match.matched_on == ["gpt-4o"]
