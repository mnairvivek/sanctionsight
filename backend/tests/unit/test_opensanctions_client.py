"""Unit tests for the OpenSanctions FtM parser.

The parser decides which records become screenable entities and how
their provenance is labelled. Both matter for regulator-facing output,
so these tests lock in the contract explicitly.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from opensanctions_client import (
    ALREADY_COVERED_DATASETS,
    ENTITY_SCHEMAS,
    iter_entities,
    load_stats,
    parse_entity,
)


def _record(**overrides):
    base = {
        "id": "ost-demo-1",
        "schema": "Organization",
        "datasets": ["au_dfat_sanctions"],
        "properties": {
            "name": ["Acme Sanctioned Holdings"],
            "alias": ["Acme SH"],
            "country": ["ru"],
            "program": ["RU-2022"],
        },
    }
    base.update(overrides)
    return base


def test_parses_organization_with_aliases_country_and_source_tag() -> None:
    parsed = parse_entity(_record())
    assert parsed is not None
    assert parsed["name"] == "Acme Sanctioned Holdings"
    assert "Acme SH" in parsed["aliases"]
    assert parsed["country"] == "ru"
    assert parsed["entity_type"] == "ORGANIZATION"
    assert parsed["list_source"] == "OPENSANCTIONS_AU"
    assert parsed["source_id"] == "ost-demo-1"
    assert parsed["programs"] == "RU-2022"


def test_person_schema_maps_to_individual() -> None:
    parsed = parse_entity(_record(schema="Person", properties={"name": ["Jane Doe"]}))
    assert parsed is not None
    assert parsed["entity_type"] == "INDIVIDUAL"


def test_unknown_schema_is_dropped() -> None:
    # Address / Identification records are metadata, not screenable actors.
    assert parse_entity(_record(schema="Address")) is None
    assert parse_entity(_record(schema="Identification")) is None


def test_record_without_name_is_dropped() -> None:
    assert parse_entity(_record(properties={"name": []})) is None
    assert parse_entity(_record(properties={})) is None


def test_record_only_in_already_covered_dataset_is_skipped() -> None:
    # If OpenSanctions only sees this entity through us_ofac_sdn, skip
    # it — our dedicated OFAC loader already owns that record.
    record = _record(datasets=["us_ofac_sdn"])
    assert parse_entity(record) is None


def test_record_in_covered_plus_extra_dataset_is_kept() -> None:
    # Present in OFAC AND an OS-exclusive dataset → still valuable (the
    # second dataset adds coverage OFAC doesn't have).
    record = _record(datasets=["us_ofac_sdn", "ch_seco_sanctions"])
    parsed = parse_entity(record)
    assert parsed is not None
    assert parsed["list_source"] == "OPENSANCTIONS_CH"


def test_already_covered_constant_is_frozen() -> None:
    # Guardrail: accidentally mutating this set at runtime would silently
    # change dedup behaviour across the whole screener.
    assert isinstance(ALREADY_COVERED_DATASETS, frozenset)
    assert isinstance(ENTITY_SCHEMAS, frozenset)


def test_aliases_are_capped_at_ten() -> None:
    many = [f"alias-{i}" for i in range(25)]
    parsed = parse_entity(_record(properties={"name": ["X"], "alias": many}))
    assert parsed is not None
    assert len(parsed["aliases"]) == 10


def test_jurisdiction_fallback_when_country_missing() -> None:
    parsed = parse_entity(_record(properties={"name": ["X"], "jurisdiction": ["ir"]}))
    assert parsed is not None
    assert parsed["country"] == "ir"


def test_programs_capped_at_three_and_joined() -> None:
    parsed = parse_entity(
        _record(properties={"name": ["X"], "program": ["A", "B", "C", "D"]})
    )
    assert parsed is not None
    assert parsed["programs"] == "A, B, C"


def test_string_values_coerced_to_list() -> None:
    # Some FtM emitters produce a bare string instead of a single-item
    # list. Accept both rather than dropping otherwise-valid records.
    parsed = parse_entity(
        _record(properties={"name": "Solo Name", "alias": "Solo Alias"})
    )
    assert parsed is not None
    assert parsed["name"] == "Solo Name"
    assert parsed["aliases"] == ["Solo Alias"]


def test_iter_entities_skips_malformed_lines(tmp_path) -> None:
    path = tmp_path / "ftm.jsonl"
    good = _record()
    bad_schema = _record(schema="Address")
    lines = [
        json.dumps(good),
        "",  # blank
        "{not valid json",  # malformed
        json.dumps(bad_schema),
        json.dumps(_record(id="ost-demo-2", properties={"name": ["Second Entity"]})),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")

    results = list(iter_entities(str(path)))
    assert len(results) == 2
    assert results[0]["name"] == "Acme Sanctioned Holdings"
    assert results[1]["name"] == "Second Entity"


def test_iter_entities_returns_empty_when_file_missing(tmp_path) -> None:
    missing = tmp_path / "nope.jsonl"
    assert list(iter_entities(str(missing))) == []


def test_load_stats_counts_accepted_and_skipped(tmp_path) -> None:
    path = tmp_path / "ftm.jsonl"
    path.write_text(
        "\n".join([
            json.dumps(_record()),
            json.dumps(_record(schema="Address")),  # skipped
            "{bad json",  # skipped
            json.dumps(_record(id="ost-demo-2", properties={"name": ["X"]})),
        ]),
        encoding="utf-8",
    )
    stats = load_stats(str(path))
    assert stats["accepted"] == 2
    assert stats["skipped"] == 2
    assert stats["total"] == 4
