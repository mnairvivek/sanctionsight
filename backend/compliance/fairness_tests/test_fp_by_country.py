"""Per-country false-positive rate cap — fairness suite.

A known failure mode in sanctions-adjacent NLP is that one country's
cultural vocabulary (Cuban sandwich, Persian rug, Damascus steel) will
over-trigger HIGH-risk flags and disproportionately affect businesses
connected to that cuisine / craft / region.

This test asserts that no single country contributes more than 30% of
the system's HIGH-tier false positives across a curated set of
known-negative inputs. If a single country exceeds the cap, the
pipeline is systematically biased against that jurisdiction and the
release is blocked until the false-positive phrase list or scoring
weights are revisited.
"""
from __future__ import annotations

from collections import Counter

import pytest

# The analyzer pulls spaCy + many heavy deps; skip cleanly if the
# environment can't run it.
spacy_module = pytest.importorskip("spacy")


# (country, negative_text). Each entry is a passage the analyzer should
# NOT flag as HIGH. The text is either a cultural false-positive trap,
# a compliance disclaimer, or an unrelated business statement that
# happens to mention a country.
FP_PROBES = [
    ("Cuba", "The cuban sandwich remains a bestseller on our lunch menu alongside cuban coffee and Caribbean plantains."),
    ("Cuba", "Our shipping policy does not deliver to Cuba under any circumstances; this page is for reference only."),
    ("Iran", "Persian rugs and Persian cats were featured in last weekend's community fair at the local park."),
    ("Iran", "The Persian calendar is lunisolar; our cultural centre hosts Nowruz celebrations each March."),
    ("Syria", "Our chef-grade knives use genuine damascus steel forged with traditional pattern-welding."),
    ("Syria", "The Syrian hamster is a popular small pet; care guides are available on our wildlife blog."),
    ("North Korea", "Our Korean BBQ restaurant chain serves bulgogi, kimchi, and Korean fried chicken."),
    ("North Korea", "Korean pop music performances are scheduled for the fall arts festival."),
    ("Russia", "Russian literature courses cover Dostoevsky, Tolstoy, and Chekhov in translation."),
    ("Russia", "The retail chain accepts payment in US dollars, euros, and British pounds across all locations."),
    ("Belarus", "Our travel blog's Belarus section covers tourist visas and exchange-rate history for visitors."),
    ("Ukraine", "Ukrainian folk music and dance workshops run monthly at the community arts centre."),
    ("Venezuela", "The Venezuelan arepa is a staple at our new Latin American food hall."),
    ("Myanmar", "Burmese cuisine features tea-leaf salad, mohinga, and curries; our food guide has recipes."),
    ("Crimea", "The history of Crimea is covered in our academic reading list for early modern Europe."),
]


MAX_COUNTRY_SHARE_OF_HIGH_FPS = 0.30
# Below this absolute count the share ratio is too noisy — a single FP
# trivially produces 100% share and the test would flap. Enforce the
# absolute-rate ceiling in ``test_overall_high_fp_rate_below_budget``
# instead and skip the share assertion.
SHARE_CAP_MIN_SAMPLE = 4


@pytest.fixture(scope="module")
def analyzer_factory():
    from sanctions_engine import get_analyzer
    return get_analyzer


def _analyze_negative(analyzer, text: str) -> str:
    """Run one probe through the analyzer and return the risk level."""
    result = analyzer.analyze_content(
        {"content": text, "type": "HTML"}, url="test://fp-probe"
    )
    return str(result.get("risk_level", "UNKNOWN")).upper()


def test_no_single_country_dominates_high_fps(analyzer_factory) -> None:
    """No jurisdiction may account for > 30% of the HIGH-tier false-positive pool."""
    high_fp_by_country: Counter = Counter()
    total_high_fps = 0

    for country, text in FP_PROBES:
        analyzer = analyzer_factory(country)
        level = _analyze_negative(analyzer, text)
        if level == "HIGH":
            high_fp_by_country[country] += 1
            total_high_fps += 1

    if total_high_fps < SHARE_CAP_MIN_SAMPLE:
        # Too few HIGH FPs to evaluate share meaningfully — one sample
        # is always 100%. The absolute-rate cap in the next test still
        # gates on overall volume.
        return

    max_country, max_count = high_fp_by_country.most_common(1)[0]
    share = max_count / total_high_fps
    assert share <= MAX_COUNTRY_SHARE_OF_HIGH_FPS, (
        f"Country '{max_country}' contributes {share:.0%} of HIGH-tier false positives "
        f"({max_count}/{total_high_fps}) — exceeds {MAX_COUNTRY_SHARE_OF_HIGH_FPS:.0%} cap. "
        f"Investigate false-positive phrase list or tier weighting for this country. "
        f"Full distribution: {dict(high_fp_by_country)}"
    )


def test_overall_high_fp_rate_below_budget(analyzer_factory) -> None:
    """Absolute HIGH-tier FP rate across all probes must be ≤ 20%.

    This is a stricter, cross-country version of the ceiling in
    ``model_card.md`` §6. If the total HIGH-FP rate creeps up the user
    trust in HIGH flags erodes even if no single country dominates.
    """
    high = 0
    for country, text in FP_PROBES:
        analyzer = analyzer_factory(country)
        if _analyze_negative(analyzer, text) == "HIGH":
            high += 1

    rate = high / len(FP_PROBES)
    assert rate <= 0.20, (
        f"Overall HIGH-tier FP rate {rate:.0%} ({high}/{len(FP_PROBES)}) exceeds 20% budget. "
        f"Release blocked until false-positive controls are revisited."
    )
