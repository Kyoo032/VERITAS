"""S1: real tokenizer service (tiktoken) and per-model pricing table.

Contract: docs/06-m2-probe-spec.md §3.5 (``TokenizerService`` — injectable
``resolve_encoding`` + ``count``). The per-model input/output rate table
replaces the M1 naive ``chars/4`` budget heuristic
(docs/05-weekend-execution-plan.md S1; docs/03-evaluation-flow.md §12).

All rates are USD **per 1K tokens** (``RATE_UNIT``), matching the S1
acceptance "pricing table maps claimed model -> per-1K rate". The table is a
deterministic static snapshot of current product-plan price assumptions
(OpenAI public pricing, Aug 2026); no network calls are made. Unknown models
fall back to the conservative gpt-4o rate (``FALLBACK_RATES``) so the budget
cost guard never silently underprices an uncatalogued claimed model, and to
``cl100k_base`` (``FALLBACK_ENCODING``) for counting.
"""

from __future__ import annotations

from dataclasses import dataclass

import tiktoken
from pydantic import BaseModel

#: Default model when a BudgetTracker/caller does not pin one (mirrors the
#: orchestrator's own fallback of ``claimed_models[0] or "gpt-4o"``).
DEFAULT_MODEL = "gpt-4o"

#: Best-effort counting encoding for models unknown to :func:`resolve_encoding`.
FALLBACK_ENCODING = "cl100k_base"

#: Unit all pricing-table rates are expressed in (USD per 1K tokens).
RATE_UNIT = "per-1K tokens"

# Explicit-priority (most-specific-first) encoding rules (§3.5). The
# ``o200k_base`` rules are checked before the broader ``gpt-4*`` rule, so
# ``gpt-4o*`` can never fall through to ``cl100k_base``.
_ENCODING_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("gpt-4o", "o1", "o3", "o4"), "o200k_base"),
    (("gpt-4", "gpt-3.5", "text-", "davinci", "curie"), "cl100k_base"),
)


def resolve_encoding(model: str) -> str | None:
    """tiktoken encoding name for the claimed model, or ``None`` if unknown.

    ``gpt-4o*``, ``o1*``, ``o3*``, ``o4*`` -> ``o200k_base``; ``gpt-4*``,
    ``gpt-3.5*``, ``text-*``, ``davinci*``, ``curie*`` -> ``cl100k_base``
    (legacy).  Matching is most-specific-prefix first: ``gpt-4o*`` never
    falls through to the broader ``gpt-4*`` rule.  Unknown models return
    ``None`` (the recount probe SKIPs with "unknown encoding for model").
    """

    name = model.strip().lower()
    for prefixes, encoding in _ENCODING_RULES:
        if any(name.startswith(prefix) for prefix in prefixes):
            return encoding
    return None


@dataclass(frozen=True)
class ModelRates:
    """USD rates for one model family, per 1K tokens (``RATE_UNIT``)."""

    input_per_1k: float
    output_per_1k: float


# Most-specific prefix first: "gpt-4o-mini" must beat "gpt-4o", "o1-mini"
# beats "o1", "o3-mini" beats "o3", "gpt-4-turbo" beats "gpt-4". Values are
# the deterministic static snapshot of current OpenAI product-plan prices
# (USD per 1K tokens, Aug 2026); legacy entries (davinci/curie/text-*) use
# their final published prices.
#
# Budget-guard assumption: "o4" has no locked row in the static snapshot, so
# it is charged at o1's high conservative rates — the cost guard must never
# underprice a claimed o4 model (the FALLBACK_RATES gpt-4o rate would be
# ~6x cheaper). The row is a conservative budget guard, not a price claim.
_PRICING_TABLE: tuple[tuple[str, ModelRates], ...] = (
    ("gpt-4o-mini", ModelRates(input_per_1k=0.00015, output_per_1k=0.0006)),
    ("gpt-4o", ModelRates(input_per_1k=0.0025, output_per_1k=0.010)),
    ("o1-mini", ModelRates(input_per_1k=0.0011, output_per_1k=0.0044)),
    ("o1", ModelRates(input_per_1k=0.015, output_per_1k=0.060)),
    ("o3-mini", ModelRates(input_per_1k=0.0011, output_per_1k=0.0044)),
    ("o3", ModelRates(input_per_1k=0.002, output_per_1k=0.008)),
    ("o4-mini", ModelRates(input_per_1k=0.0011, output_per_1k=0.0044)),
    ("o4", ModelRates(input_per_1k=0.015, output_per_1k=0.060)),
    ("gpt-4-turbo", ModelRates(input_per_1k=0.010, output_per_1k=0.030)),
    ("gpt-4", ModelRates(input_per_1k=0.030, output_per_1k=0.060)),
    ("gpt-3.5", ModelRates(input_per_1k=0.0005, output_per_1k=0.0015)),
    ("davinci", ModelRates(input_per_1k=0.020, output_per_1k=0.020)),
    ("curie", ModelRates(input_per_1k=0.002, output_per_1k=0.002)),
    ("text-", ModelRates(input_per_1k=0.002, output_per_1k=0.002)),
)

#: Conservative default for unknown models: the gpt-4o rate.
FALLBACK_RATES = ModelRates(input_per_1k=0.0025, output_per_1k=0.010)


def resolve_rates(model: str) -> ModelRates | None:
    """Most-specific-prefix per-1K rates for a claimed model, or ``None``.

    ``"gpt-4o-mini"`` matches the mini entry, never the broader ``gpt-4o``
    entry; unknown models return ``None`` (callers pick ``FALLBACK_RATES``).
    """

    name = model.strip().lower()
    for prefix, rates in _PRICING_TABLE:
        if name.startswith(prefix):
            return rates
    return None


_ENCODING_CACHE: dict[str, tiktoken.Encoding] = {}


def count(text: str, encoding: str) -> int:
    """tiktoken BPE count of ``text`` under the named ``encoding``.

    Encoding objects are cached after first load (tiktoken downloads the BPE
    file once per encoding).  An unknown encoding name raises ``ValueError``
    from tiktoken — callers resolve the name first via
    :func:`resolve_encoding`.
    """

    enc = _ENCODING_CACHE.get(encoding)
    if enc is None:
        enc = tiktoken.get_encoding(encoding)
        _ENCODING_CACHE[encoding] = enc
    return len(enc.encode(text))


class TokenizerService(BaseModel):
    """tiktoken-backed tokenizer service (docs/06 §3.5) — injectable.

    Stateless (encodings are cached module-wide); subclasses can override
    either method for deterministic doubles in tests.
    """

    def resolve_encoding(self, model: str) -> str | None:
        return resolve_encoding(model)

    def count(self, text: str, encoding: str) -> int:
        return count(text, encoding)
