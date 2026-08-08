"""S1: tiktoken tokenizer service, per-model pricing, BudgetTracker accounting.

Covers docs/05 §6 S1 acceptance: tiktoken recount matches known token counts
for a fixture battery; the pricing table maps claimed model -> per-1K rate;
BudgetTracker no longer uses chars/4 (no PRICE_PER_1K / prompt_chars /
completion_chars) and tracks prompt_tokens / completion_tokens / requests /
estimated_usd / blocked with separate input and output rates.
"""

from __future__ import annotations

import pytest

from supgate.models import BudgetTracker
from supgate.tokenizers import (
    DEFAULT_MODEL,
    FALLBACK_ENCODING,
    FALLBACK_RATES,
    RATE_UNIT,
    ModelRates,
    TokenizerService,
    count,
    resolve_encoding,
    resolve_rates,
)

# --- encoding resolution (docs/06 §3.5): most-specific prefix first --------

@pytest.mark.parametrize(
    ("model", "encoding"),
    [
        ("gpt-4o", "o200k_base"),
        ("gpt-4o-mini", "o200k_base"),
        ("gpt-4o-custom-v2026", "o200k_base"),
        ("o1", "o200k_base"),
        ("o1-mini", "o200k_base"),
        ("o3", "o200k_base"),
        ("o3-mini", "o200k_base"),
        ("o4-mini", "o200k_base"),
        ("gpt-4", "cl100k_base"),
        ("gpt-4-turbo", "cl100k_base"),
        ("gpt-3.5-turbo", "cl100k_base"),
        ("text-davinci-003", "cl100k_base"),
        ("davinci", "cl100k_base"),
        ("davinci-002", "cl100k_base"),
        ("curie", "cl100k_base"),
        ("", None),
        ("unknown-model", None),
        ("claude-3-5-sonnet", None),
    ],
)
def test_resolve_encoding_most_specific_prefix(model: str, encoding: str | None) -> None:
    assert resolve_encoding(model) == encoding


def test_resolve_encoding_gpt4o_never_falls_through_to_gpt4() -> None:
    # the S1 regression case: gpt-4o* must not match the broader gpt-4* rule
    assert resolve_encoding("gpt-4o") == "o200k_base"
    assert resolve_encoding("gpt-4o-mini") == "o200k_base"
    assert resolve_encoding("gpt-4") == "cl100k_base"


def test_resolve_encoding_normalizes_case_and_whitespace() -> None:
    assert resolve_encoding("  GPT-4O  ") == "o200k_base"
    assert resolve_encoding("O1-mini") == "o200k_base"


# --- token counting: known fixture battery ---------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("Hello world", 2),
        ("hi", 1),
        ("The quick brown fox jumps over the lazy dog", 9),
    ],
)
def test_count_matches_known_fixture_counts(text: str, expected: int) -> None:
    assert count(text, "cl100k_base") == expected
    assert count(text, "o200k_base") == expected


def test_count_differs_between_encodings() -> None:
    # fixture proving encoding selection changes the recount (d4.recount_deviation)
    text = "emoji test 🚀🚀🚀 and more"
    assert count(text, "cl100k_base") == 13
    assert count(text, "o200k_base") == 10


def test_count_unknown_encoding_raises_value_error() -> None:
    with pytest.raises(ValueError):
        count("hello", "no_such_encoding")


def test_tokenizer_service_implements_contract() -> None:
    service = TokenizerService()
    assert service.resolve_encoding("gpt-4o") == "o200k_base"
    assert service.resolve_encoding("o1") == "o200k_base"
    assert service.resolve_encoding("gpt-3.5-turbo") == "cl100k_base"
    assert service.resolve_encoding("unknown-model") is None
    assert service.count("Hello world", "cl100k_base") == 2


# --- pricing table: per-1K rates, most-specific model matching -------------

def test_rate_unit_is_per_1k_tokens() -> None:
    assert RATE_UNIT == "per-1K tokens"


def test_rates_most_specific_model_matching() -> None:
    assert resolve_rates("gpt-4o") == ModelRates(0.0025, 0.010)
    assert resolve_rates("gpt-4o-mini") == ModelRates(0.00015, 0.0006)
    assert resolve_rates("o1") == ModelRates(0.015, 0.060)
    assert resolve_rates("o1-mini") == ModelRates(0.0011, 0.0044)
    assert resolve_rates("o3") == ModelRates(0.002, 0.008)
    assert resolve_rates("o3-mini") == ModelRates(0.0011, 0.0044)
    assert resolve_rates("o4") == ModelRates(0.015, 0.060)
    assert resolve_rates("o4-mini") == ModelRates(0.0011, 0.0044)
    assert resolve_rates("gpt-4-turbo") == ModelRates(0.010, 0.030)
    assert resolve_rates("gpt-4") == ModelRates(0.030, 0.060)
    assert resolve_rates("gpt-3.5-turbo") == ModelRates(0.0005, 0.0015)
    assert resolve_rates("text-davinci-003") == ModelRates(0.002, 0.002)
    assert resolve_rates("no-such-model") is None
    assert resolve_rates("") is None


def test_fallback_rates_are_the_gpt4o_rate() -> None:
    assert DEFAULT_MODEL == "gpt-4o"
    assert resolve_encoding(DEFAULT_MODEL) == "o200k_base"
    assert resolve_rates("gpt-4o") == FALLBACK_RATES
    assert FALLBACK_ENCODING == "cl100k_base"


# --- BudgetTracker: real token accounting (S1) -----------------------------

def test_budget_default_construction_counts_real_tokens() -> None:
    budget = BudgetTracker()
    budget.add("Hello world", "hi")
    assert budget.requests == 1
    assert budget.prompt_tokens == 2  # o200k for DEFAULT_MODEL ("gpt-4o")
    assert budget.completion_tokens == 1
    assert budget.estimated_usd == pytest.approx((2 * 0.0025 + 1 * 0.010) / 1000)
    assert not budget.blocked
    assert budget.remaining() == float("inf")


def test_budget_counts_and_prices_with_separate_input_output_rates() -> None:
    budget = BudgetTracker(budget_usd=100.0, model="gpt-4o")
    prompt = "Hello world"
    completion = "The quick brown fox jumps over the lazy dog"
    budget.add(prompt, completion)
    assert budget.requests == 1
    assert budget.prompt_tokens == count(prompt, "o200k_base")
    assert budget.completion_tokens == count(completion, "o200k_base")
    expected = (2 * 0.0025 + 9 * 0.010) / 1000  # input rate != output rate
    assert budget.estimated_usd == pytest.approx(expected)


def test_budget_model_override_per_add() -> None:
    budget = BudgetTracker(model="gpt-4o")
    budget.add("Hello world", "", model="o1")  # same tokens, different price
    assert budget.prompt_tokens == 2
    assert budget.estimated_usd == pytest.approx(2 * 0.015 / 1000)


def test_budget_unknown_model_uses_fallback_encoding_and_rate() -> None:
    budget = BudgetTracker(model="mystery-llm")
    assert resolve_encoding("mystery-llm") is None
    assert resolve_rates("mystery-llm") is None
    budget.add("Hello world", "")
    assert budget.prompt_tokens == 2  # counted under FALLBACK_ENCODING
    assert budget.estimated_usd == pytest.approx(2 * FALLBACK_RATES.input_per_1k / 1000)


def test_budget_accumulates_across_requests() -> None:
    budget = BudgetTracker(budget_usd=0.001, model="gpt-4o")
    budget.add("hi", "hi")
    budget.add("hi", "hi")
    assert budget.requests == 2
    assert budget.prompt_tokens == 2
    assert budget.completion_tokens == 2
    assert budget.estimated_usd == pytest.approx(2 * (1 * 0.0025 + 1 * 0.010) / 1000)
    assert not budget.blocked
    assert budget.remaining() == pytest.approx(0.001 - budget.estimated_usd)


def test_budget_blocks_once_cap_reached() -> None:
    budget = BudgetTracker(budget_usd=0.00009, model="gpt-4o")
    budget.add("Hello world", "The quick brown fox jumps over the lazy dog")
    assert budget.estimated_usd == pytest.approx(0.000095)
    assert budget.blocked
    assert budget.remaining() == 0.0


def test_budget_accepts_tokenizer_double() -> None:
    class FixedTokenizer(TokenizerService):
        n: int = 3

        def resolve_encoding(self, model: str) -> str | None:
            return "fixed"

        def count(self, text: str, encoding: str) -> int:
            return self.n

    budget = BudgetTracker(budget_usd=10.0, model="gpt-4o", tokenizer=FixedTokenizer(n=7))
    budget.add("aaa", "bbb")
    assert budget.prompt_tokens == 7
    assert budget.completion_tokens == 7
    assert budget.estimated_usd == pytest.approx((7 * 0.0025 + 7 * 0.010) / 1000)


def test_budget_accepts_explicit_rates_override() -> None:
    budget = BudgetTracker(model="gpt-4o", rates=ModelRates(0.001, 0.002))
    budget.add("Hello world", "hi")
    assert budget.estimated_usd == pytest.approx((2 * 0.001 + 1 * 0.002) / 1000)


def test_budget_summary_roundtrips_schema2_fields() -> None:
    budget = BudgetTracker(budget_usd=1.0, model="gpt-4o")
    budget.add("Hello world", "The quick brown fox jumps over the lazy dog")
    summary = budget.summary()
    assert summary.prompt_tokens == 2
    assert summary.completion_tokens == 9
    assert summary.requests == 1
    assert summary.estimated_usd == pytest.approx(budget.estimated_usd)
    assert summary.blocked is False
    assert summary.model == "gpt-4o"
    dumped = summary.model_dump()  # schema-2 serializable (docs/08 §3)
    assert dumped["prompt_tokens"] == 2
    assert dumped["completion_tokens"] == 9
    assert dumped["estimated_usd"] == pytest.approx(budget.estimated_usd)
