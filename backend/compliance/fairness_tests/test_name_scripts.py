"""Name-script coverage — fairness suite.

Sanctions screeners have a well-documented failure mode: the matching
algorithm works well on Latin-script names the test dataset was built
around, and silently degrades on Cyrillic, Arabic, and CJK inputs. A
regression where a rapidfuzz tokeniser change tanks Arabic match rates
(for instance) must not ship.

These tests lock in a minimum match rate per script. The sanctioned
names are fictional — the point is to exercise the matcher, not to
litigate real designations.
"""
from __future__ import annotations

import pytest

pytest.importorskip("rapidfuzz")

from sanctions_list_screener import SanctionsListScreener


# Per-script fixture: (listed_name, aliases). Queries probe matching
# under realistic user-input variations (punctuation loss, spacing,
# transliteration drift, word order).
SCRIPT_FIXTURES = {
    "latin": [
        ("Acme Sanctioned Holdings Ltd", ["Acme Holdings", "ASH Ltd"]),
        ("Global North Trading Company", ["GNT Company", "Global North Trading"]),
        ("Maritime Shipping International", ["MSI"]),
    ],
    "cyrillic": [
        ("Газпромбанк", ["Gazprombank", "ОАО Газпромбанк"]),
        ("Сбербанк России", ["Sberbank", "Сбербанк"]),
        ("Новатэк", ["Novatek", "ОАО Новатэк"]),
    ],
    "arabic": [
        ("بنك ملي إيران", ["Bank Melli Iran", "Melli Bank"]),
        ("بنك صادرات إيران", ["Bank Saderat Iran"]),
        ("شركة النفط الوطنية الإيرانية", ["National Iranian Oil Company", "NIOC"]),
    ],
    "chinese_pinyin": [
        ("中国石油天然气集团", ["China National Petroleum", "CNPC"]),
        ("中国船舶重工集团", ["China Shipbuilding Industry Corporation", "CSIC"]),
        ("朝鲜光鲜集团", ["Chosun Gwangson Group", "Kwangson"]),
    ],
}


# Probes: for each listed entity, what would an analyst realistically
# paste in? Each tuple is (listed_name_key, query_variant).
PROBES = {
    "latin": [
        ("Acme Sanctioned Holdings Ltd", "acme sanctioned holdings"),
        ("Acme Sanctioned Holdings Ltd", "ACME HOLDINGS"),
        ("Global North Trading Company", "global north trading co."),
        ("Maritime Shipping International", "maritime shipping intl"),
    ],
    "cyrillic": [
        ("Газпромбанк", "газпромбанк"),
        ("Газпромбанк", "Gazprombank"),
        ("Сбербанк России", "сбербанк"),
        ("Новатэк", "Novatek"),
    ],
    "arabic": [
        ("بنك ملي إيران", "بنك ملي"),
        ("بنك ملي إيران", "Bank Melli Iran"),
        ("شركة النفط الوطنية الإيرانية", "NIOC"),
    ],
    "chinese_pinyin": [
        ("中国石油天然气集团", "CNPC"),
        ("中国石油天然气集团", "China National Petroleum"),
        ("中国船舶重工集团", "CSIC"),
    ],
}


# Release floor per script: fraction of probes that must match above the
# default screening threshold (82). Bump these only with a Tier A
# governance event; relaxing them silently hides a fairness regression.
MATCH_RATE_FLOOR = {
    "latin": 1.00,
    "cyrillic": 0.75,
    "arabic": 0.66,
    "chinese_pinyin": 0.66,
}


def _build_screener(tmp_path) -> SanctionsListScreener:
    """Construct an empty screener and load fixture entities directly."""
    screener = SanctionsListScreener(str(tmp_path))  # empty dir = 0 entities loaded
    for script, entities in SCRIPT_FIXTURES.items():
        for name, aliases in entities:
            screener._add_entity(
                name=name,
                aliases=list(aliases),
                list_source=f"TEST_{script.upper()}",
                entity_type="ORGANIZATION",
                country="",
                programs="",
                source_id=f"test-{name[:8]}",
            )
    screener._build_name_index()
    return screener


@pytest.fixture(scope="module")
def screener(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fairness_data")
    return _build_screener(tmp)


@pytest.mark.parametrize("script", list(SCRIPT_FIXTURES.keys()))
def test_per_script_match_rate_above_floor(screener, script) -> None:
    """For each script, at least MATCH_RATE_FLOOR of probes must hit."""
    probes = PROBES[script]
    floor = MATCH_RATE_FLOOR[script]

    hits = 0
    for listed_name, query in probes:
        matches = screener.screen(query, threshold=82)
        matched_names = [m["listed_name"] for m in matches]
        if listed_name in matched_names:
            hits += 1

    rate = hits / len(probes)
    assert rate >= floor, (
        f"[{script}] match rate {rate:.2f} below floor {floor:.2f} "
        f"({hits}/{len(probes)} probes matched)"
    )


def test_no_script_falls_below_latin_minus_margin(screener) -> None:
    """No non-Latin script may fall more than 0.35 below Latin.

    Guards against the failure mode where Latin-script tests pass
    perfectly and a non-Latin regression hides in the noise.
    """
    rates = {}
    for script, probes in PROBES.items():
        hits = sum(
            1 for listed, q in probes
            if listed in [m["listed_name"] for m in screener.screen(q, threshold=82)]
        )
        rates[script] = hits / len(probes)

    latin_rate = rates["latin"]
    for script, rate in rates.items():
        gap = latin_rate - rate
        assert gap <= 0.35, (
            f"[{script}] lags Latin by {gap:.2f} ({rate:.2f} vs {latin_rate:.2f}) — "
            f"fairness regression; investigate tokenisation / normalisation"
        )
