"""OpenSanctions bulk-dataset client.

Parses the OpenSanctions consolidated Follow-The-Money (FtM) JSONL feed
into the entity-dict shape the screener expects. Extracted from
`sanctions_list_screener._load_opensanctions` so the parser is a
dedicated, testable unit and so the screener's list-loading surface is
smaller.

OFSI (UK), OFAC, UN and EU loaders intentionally stay on their own
parsers — their source formats have quirks worth preserving even when
the same entities are redundantly present in the OpenSanctions bulk
dataset. We de-duplicate those below via ``ALREADY_COVERED_DATASETS``.

Licence note: the OpenSanctions consolidated sanctions dataset is
CC BY-NC 4.0. The tool's evidence packets cite ``list_source`` per
match so the licence attribution is preserved downstream.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, Iterator, List, Optional

logger = logging.getLogger("opensanctions_client")


# FtM schemas we accept. Everything else (Address, Identification,
# Membership, etc.) is auxiliary metadata, not an actor we should screen
# a name against.
ENTITY_SCHEMAS = frozenset({
    "Person",
    "LegalEntity",
    "Organization",
    "Company",
    "PublicBody",
    "Vessel",
    "Aircraft",
})


# Datasets we already load via dedicated parsers. If an OpenSanctions
# entity appears ONLY in these datasets we skip it to avoid double
# counting (and to keep the source provenance attached to the richer
# upstream loader).
ALREADY_COVERED_DATASETS = frozenset({
    "us_ofac_sdn",
    "us_ofac_cons",
    "un_sc",
    "gb_hmt_sanctions",
    "eu_fsf",
})


# Preferred provenance label for a handful of high-signal single-source
# datasets. Everything else falls back to the generic OPENSANCTIONS tag.
DATASET_SOURCE_MAP = {
    "au_dfat_sanctions": "OPENSANCTIONS_AU",
    "ca_dfatd_sema_sanctions": "OPENSANCTIONS_CA",
    "ch_seco_sanctions": "OPENSANCTIONS_CH",
    "jp_mof_sanctions": "OPENSANCTIONS_JP",
    "interpol_red_notices": "INTERPOL",
}


def _coerce_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def parse_entity(raw: dict) -> Optional[dict]:
    """Normalise one FtM record into the screener entity dict.

    Returns ``None`` when the record should be skipped: wrong schema,
    no usable name, or it's already covered by one of our dedicated
    upstream loaders.
    """
    if not isinstance(raw, dict):
        return None

    schema = raw.get("schema", "")
    if schema not in ENTITY_SCHEMAS:
        return None

    props = raw.get("properties") or {}
    if not isinstance(props, dict):
        return None

    names = _coerce_list(props.get("name"))
    primary_name = names[0].strip() if names else ""
    if not primary_name:
        return None

    aliases: List[str] = []
    for field in ("alias", "weakAlias", "previousName"):
        aliases.extend(_coerce_list(props.get(field)))

    countries = _coerce_list(props.get("country")) or _coerce_list(props.get("jurisdiction"))
    country = countries[0] if countries else ""

    datasets = _coerce_list(raw.get("datasets"))
    if datasets and all(d in ALREADY_COVERED_DATASETS for d in datasets):
        return None

    list_source = "OPENSANCTIONS"
    for ds in datasets:
        if ds in DATASET_SOURCE_MAP:
            list_source = DATASET_SOURCE_MAP[ds]
            break

    entity_type = "INDIVIDUAL" if schema == "Person" else "ORGANIZATION"

    programs_list = _coerce_list(props.get("program")) or _coerce_list(props.get("sanction"))
    programs = ", ".join(programs_list[:3])

    return {
        "name": primary_name,
        "aliases": aliases[:10],
        "list_source": list_source,
        "entity_type": entity_type,
        "country": country,
        "programs": programs,
        "source_id": str(raw.get("id") or ""),
    }


def iter_entities(path: str) -> Iterator[dict]:
    """Stream parsed entities from the FtM JSONL file at ``path``.

    Malformed lines are silently skipped so one bad record can't abort
    the whole load — the bulk file has ~97K lines and a partial load is
    strictly better than none.
    """
    if not os.path.isfile(path):
        logger.info("OpenSanctions file not found: %s", path)
        return

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed = parse_entity(raw)
            if parsed is not None:
                yield parsed


def load_stats(path: str) -> Dict[str, int]:
    """Return ``{accepted, skipped, total}`` counts for the FtM file.

    Used by the doctor endpoint so operators can confirm the dataset
    has loaded after a refresh. Walks the file once; cheap enough to
    call on demand for a ~200MB JSONL.
    """
    accepted = 0
    skipped = 0
    if not os.path.isfile(path):
        return {"accepted": 0, "skipped": 0, "total": 0}

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if parse_entity(raw) is None:
                skipped += 1
            else:
                accepted += 1
    return {"accepted": accepted, "skipped": skipped, "total": accepted + skipped}
