"""
Sanctions List Screener
-------------------------
Loads downloaded sanctions lists (OFAC SDN, EU, UN, OFSI) and performs
fuzzy name matching against them using rapidfuzz.

Usage:
    from sanctions_list_screener import SanctionsListScreener
    screener = SanctionsListScreener("data")
    matches = screener.screen("Acme Trading Corp", threshold=82)
"""

import csv
import hashlib
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz, process

logger = logging.getLogger("sanctions_screener")


def _file_sha256(path: str, chunk_size: int = 65536) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                h.update(chunk)
    except OSError as exc:
        logger.warning("Could not hash %s: %s", path, exc)
        return None
    return h.hexdigest()


def _record_list_snapshots(data_dir: str, loaders, counts: Dict[str, int]) -> None:
    """Write one ListSnapshot row per loaded sanctions list. Best-effort."""
    try:
        import storage  # lazy import — screener runs without DB too
    except Exception as exc:
        logger.debug("List snapshot recording skipped (storage unavailable: %s)", exc)
        return

    try:
        with storage.get_session() as session:
            for list_name, fname, _loader in loaders:
                path = os.path.join(data_dir, fname)
                digest = _file_sha256(path)
                if not digest:
                    continue
                existing = session.query(storage.ListSnapshot).filter_by(
                    list_name=list_name, sha256=digest,
                ).one_or_none()
                if existing:
                    continue
                downloaded_at = datetime.utcfromtimestamp(os.path.getmtime(path))
                snapshot = storage.ListSnapshot(
                    list_name=list_name,
                    downloaded_at=downloaded_at,
                    sha256=digest,
                    entity_count=counts.get(list_name, 0),
                    path=path,
                    active_from=datetime.utcnow(),
                )
                session.add(snapshot)
            session.commit()
    except Exception as exc:
        logger.warning("Failed to record list snapshots: %s", exc)


class SanctionsListScreener:
    """
    Loads sanctions lists from CSV/XML files and performs fuzzy matching.
    Designed to be instantiated once and reused across requests.
    """

    def __init__(self, data_dir: str = "data"):
        self.data_dir = data_dir
        self.entities: List[dict] = []
        self.name_index: List[Tuple[str, int]] = []  # (normalized_name, entity_index)
        self._last_updated: Optional[str] = None

        if not os.path.isdir(data_dir):
            logger.warning("Data directory not found: %s — no sanctions lists loaded.", data_dir)
            return

        # Track per-list entity counts so we can record ListSnapshot rows
        # below once loading is complete.
        self._list_counts: Dict[str, int] = {}
        self._loaders = [
            ("ofac_sdn", "ofac_sdn.csv", self._load_ofac_sdn),
            ("ofac_alt", "ofac_sdn_alt.csv", self._load_ofac_alt),
            ("ofac_consolidated", "ofac_consolidated.csv", self._load_ofac_consolidated),
            ("un_consolidated", "un_consolidated.xml", self._load_un_list),
            ("uk_sanctions", "UK-Sanctions-List.csv", self._load_uk_list),
            ("eu_sanctions", "eu_sanctions.csv", self._load_eu_list),
            ("us_csl", "us_csl.json", self._load_us_csl),
            ("opensanctions", "opensanctions_sanctions.jsonl", self._load_opensanctions),
        ]
        for list_name, expected_file, loader in self._loaders:
            before = len(self.entities)
            try:
                loader()
            except Exception as exc:
                logger.warning("List loader %s failed: %s", list_name, exc)
            self._list_counts[list_name] = len(self.entities) - before

        # Build the name index for fast matching
        self._build_name_index()

        # Read last updated timestamp
        ts_file = os.path.join(data_dir, "_last_updated.txt")
        if os.path.exists(ts_file):
            with open(ts_file, "r") as f:
                self._last_updated = f.read().strip()

        logger.info(
            "Sanctions screener loaded: %d entities, %d searchable names. Last updated: %s",
            len(self.entities), len(self.name_index), self._last_updated or "unknown",
        )

        # Phase 1: record a ListSnapshot row per loaded list for regulatory
        # traceability. Each finding will join against the snapshot active
        # at analysis time. Best-effort — if storage isn't installed yet,
        # loading still succeeds.
        _record_list_snapshots(data_dir, self._loaders, self._list_counts)

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    def _add_entity(
        self,
        name: str,
        aliases: List[str],
        list_source: str,
        entity_type: str = "UNKNOWN",
        country: str = "",
        programs: str = "",
        remarks: str = "",
        source_id: str = "",
    ):
        """Add a sanitized entity to the internal store."""
        name = (name or "").strip()
        if not name or len(name) < 2:
            return
        # Skip entries that are clearly not entity names
        if name.startswith("-") or name.isdigit():
            return

        self.entities.append({
            "name": name,
            "aliases": [a.strip() for a in aliases if a.strip() and len(a.strip()) >= 2],
            "list_source": list_source,
            "entity_type": entity_type,
            "country": country,
            "programs": programs,
            "remarks": remarks,
            "source_id": source_id,
        })

    def _load_ofac_sdn(self):
        """Load OFAC SDN list (sdn.csv)."""
        filepath = os.path.join(self.data_dir, "ofac_sdn.csv")
        if not os.path.exists(filepath):
            logger.info("OFAC SDN file not found: %s", filepath)
            return

        count = 0
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) < 3:
                        continue
                    ent_num = row[0].strip()
                    name = row[1].strip()
                    sdn_type = row[2].strip() if len(row) > 2 else ""
                    programs = row[3].strip() if len(row) > 3 else ""
                    remarks = row[11].strip() if len(row) > 11 else ""

                    entity_type = "INDIVIDUAL" if "individual" in sdn_type.lower() else "ORGANIZATION"

                    # Extract country from remarks if present
                    country = ""
                    if remarks:
                        for marker in ["Nationality ", "Country ", "citizen "]:
                            if marker in remarks:
                                idx = remarks.index(marker) + len(marker)
                                country = remarks[idx:].split(";")[0].split(".")[0].strip()
                                break

                    self._add_entity(
                        name=name,
                        aliases=[],  # Aliases come from alt.csv
                        list_source="OFAC_SDN",
                        entity_type=entity_type,
                        country=country,
                        programs=programs,
                        remarks=remarks,
                        source_id=ent_num,
                    )
                    count += 1
        except Exception as exc:
            logger.error("Error loading OFAC SDN: %s", exc)

        logger.info("Loaded %d entries from OFAC SDN", count)

    def _load_ofac_alt(self):
        """Load OFAC alternate names (alt.csv) and attach to existing SDN entries."""
        filepath = os.path.join(self.data_dir, "ofac_sdn_alt.csv")
        if not os.path.exists(filepath):
            logger.info("OFAC alt names file not found: %s", filepath)
            return

        # Build a map of ent_num -> entity index for quick lookup
        ent_map: Dict[str, List[int]] = {}
        for i, ent in enumerate(self.entities):
            if ent["list_source"] == "OFAC_SDN" and ent["source_id"]:
                ent_map.setdefault(ent["source_id"], []).append(i)

        count = 0
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) < 4:
                        continue
                    ent_num = row[0].strip()
                    alt_name = row[3].strip()
                    if ent_num in ent_map and alt_name:
                        for idx in ent_map[ent_num]:
                            self.entities[idx]["aliases"].append(alt_name)
                        count += 1
        except Exception as exc:
            logger.error("Error loading OFAC alt names: %s", exc)

        logger.info("Loaded %d alternate names from OFAC", count)

    def _load_ofac_consolidated(self):
        """Load OFAC Consolidated Non-SDN list."""
        filepath = os.path.join(self.data_dir, "ofac_consolidated.csv")
        if not os.path.exists(filepath):
            logger.info("OFAC consolidated file not found: %s", filepath)
            return

        count = 0
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                for row in reader:
                    if len(row) < 2:
                        continue
                    ent_num = row[0].strip()
                    name = row[1].strip()
                    sdn_type = row[2].strip() if len(row) > 2 else ""
                    programs = row[3].strip() if len(row) > 3 else ""

                    entity_type = "INDIVIDUAL" if "individual" in sdn_type.lower() else "ORGANIZATION"

                    self._add_entity(
                        name=name,
                        aliases=[],
                        list_source="OFAC_CONS",
                        entity_type=entity_type,
                        programs=programs,
                        source_id=ent_num,
                    )
                    count += 1
        except Exception as exc:
            logger.error("Error loading OFAC consolidated: %s", exc)

        logger.info("Loaded %d entries from OFAC Consolidated", count)

    def _load_un_list(self):
        """Load UN Security Council consolidated sanctions list (XML)."""
        filepath = os.path.join(self.data_dir, "un_consolidated.xml")
        if not os.path.exists(filepath):
            logger.info("UN list file not found: %s", filepath)
            return

        count = 0
        try:
            tree = ET.parse(filepath)
            root = tree.getroot()

            # Handle XML namespace if present
            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag.split("}")[0] + "}"

            # Try both INDIVIDUALS and ENTITIES sections
            for section_tag in ["INDIVIDUALS", "ENTITIES"]:
                section = root.find(f".//{ns}{section_tag}")
                if section is None:
                    continue

                entity_type = "INDIVIDUAL" if section_tag == "INDIVIDUALS" else "ORGANIZATION"
                child_tag = "INDIVIDUAL" if section_tag == "INDIVIDUALS" else "ENTITY"

                for entry in section.findall(f"{ns}{child_tag}"):
                    # Build name from parts
                    first = self._xml_text(entry, f"{ns}FIRST_NAME")
                    second = self._xml_text(entry, f"{ns}SECOND_NAME")
                    third = self._xml_text(entry, f"{ns}THIRD_NAME")
                    fourth = self._xml_text(entry, f"{ns}FOURTH_NAME")

                    if entity_type == "ORGANIZATION":
                        first = self._xml_text(entry, f"{ns}FIRST_NAME") or ""

                    name_parts = [p for p in [first, second, third, fourth] if p]
                    name = " ".join(name_parts).strip()

                    if not name:
                        continue

                    # Get aliases
                    aliases = []
                    for alias_elem in entry.findall(f".//{ns}ALIAS"):
                        alias_name = self._xml_text(alias_elem, f"{ns}ALIAS_NAME")
                        if alias_name:
                            aliases.append(alias_name)

                    # Get nationality
                    country = ""
                    nat_elem = entry.find(f".//{ns}NATIONALITY/{ns}VALUE")
                    if nat_elem is not None and nat_elem.text:
                        country = nat_elem.text.strip()

                    ref_num = self._xml_text(entry, f"{ns}DATAID") or ""
                    comments = self._xml_text(entry, f"{ns}COMMENTS1") or ""

                    self._add_entity(
                        name=name,
                        aliases=aliases,
                        list_source="UN_SC",
                        entity_type=entity_type,
                        country=country,
                        remarks=comments,
                        source_id=ref_num,
                    )
                    count += 1

        except Exception as exc:
            logger.error("Error loading UN list: %s", exc)

        logger.info("Loaded %d entries from UN Security Council list", count)

    def _load_uk_list(self):
        """Load OFSI UK sanctions list (CSV)."""
        filepath = os.path.join(self.data_dir, "uk_sanctions.csv")
        if not os.path.exists(filepath):
            logger.info("UK sanctions file not found: %s", filepath)
            return

        count = 0
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # UK list uses various column names — try common ones
                    name = (
                        row.get("Name 6", "") or  # Organization name
                        row.get("name6", "") or
                        ""
                    ).strip()

                    if not name:
                        # Try constructing from individual name parts
                        parts = []
                        for key in ["Name 1", "Name 2", "Name 3", "Name 4", "Name 5", "name1", "name2", "name3", "name4", "name5"]:
                            val = row.get(key, "").strip()
                            if val:
                                parts.append(val)
                        name = " ".join(parts).strip()

                    if not name:
                        continue

                    group_type = row.get("Group Type", row.get("group_type", "")).strip()
                    entity_type = "INDIVIDUAL" if "individual" in group_type.lower() else "ORGANIZATION"

                    country = row.get("Country", row.get("country", "")).strip()
                    regime = row.get("Regime", row.get("regime", "")).strip()
                    uid = row.get("Group ID", row.get("group_id", "")).strip()

                    # Aliases from alias columns
                    aliases = []
                    for i in range(1, 7):
                        for prefix in [f"Alias {i}", f"alias{i}"]:
                            alias = row.get(prefix, "").strip()
                            if alias:
                                aliases.append(alias)

                    self._add_entity(
                        name=name,
                        aliases=aliases,
                        list_source="OFSI_UK",
                        entity_type=entity_type,
                        country=country,
                        programs=regime,
                        source_id=uid,
                    )
                    count += 1

        except Exception as exc:
            logger.error("Error loading UK sanctions list: %s", exc)

        logger.info("Loaded %d entries from OFSI UK list", count)

    def _load_eu_list(self):
        """Load EU consolidated sanctions list (CSV)."""
        filepath = os.path.join(self.data_dir, "eu_sanctions.csv")
        if not os.path.exists(filepath):
            logger.info("EU sanctions file not found: %s", filepath)
            return

        count = 0
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                # Try to detect delimiter
                sample = f.read(2048)
                f.seek(0)
                delimiter = ";" if sample.count(";") > sample.count(",") else ","

                reader = csv.DictReader(f, delimiter=delimiter)
                for row in reader:
                    # EU list column names vary — try common patterns
                    name = ""
                    for key in ["NameAlias_WholeName", "wholeName", "Entity_SubjectType_name",
                                "NameAlias_wholeName", "name", "Name"]:
                        name = row.get(key, "").strip()
                        if name:
                            break

                    if not name:
                        continue

                    subject_type = ""
                    for key in ["Entity_SubjectType_code", "subjectType", "SubjectType"]:
                        subject_type = row.get(key, "").strip()
                        if subject_type:
                            break

                    entity_type = (
                        "INDIVIDUAL" if "person" in subject_type.lower() or "individual" in subject_type.lower()
                        else "ORGANIZATION"
                    )

                    country = ""
                    for key in ["Entity_SubjectType_country", "country", "Country"]:
                        country = row.get(key, "").strip()
                        if country:
                            break

                    programme = ""
                    for key in ["Entity_Regulation_programme", "programme", "Programme"]:
                        programme = row.get(key, "").strip()
                        if programme:
                            break

                    ref_num = ""
                    for key in ["Entity_LogicalId", "logicalId", "EU reference number"]:
                        ref_num = row.get(key, "").strip()
                        if ref_num:
                            break

                    self._add_entity(
                        name=name,
                        aliases=[],
                        list_source="EU",
                        entity_type=entity_type,
                        country=country,
                        programs=programme,
                        source_id=ref_num,
                    )
                    count += 1

        except Exception as exc:
            logger.error("Error loading EU sanctions list: %s", exc)

        logger.info("Loaded %d entries from EU sanctions list", count)

    def _load_us_csl(self):
        """
        Load US Consolidated Screening List (trade.gov).
        Covers: BIS Entity List, Denied Persons, Unverified, MEU, State Nonproliferation,
                State AECA Debarred, OFAC FSE/SSI/CAPTA and more.
        Downloaded by update_lists.py as us_csl.json (preferred) or us_csl.csv.
        """
        json_path = os.path.join(self.data_dir, "us_csl.json")
        csv_path = os.path.join(self.data_dir, "us_csl.csv")

        # --- JSON version (richer data) ---
        if os.path.exists(json_path):
            count = 0
            try:
                import json as _json
                with open(json_path, "r", encoding="utf-8", errors="replace") as f:
                    data = _json.load(f)

                for entry in data.get("results", []):
                    name = (entry.get("name") or "").strip()
                    if not name:
                        continue

                    source = entry.get("source", "CSL")
                    # Map trade.gov source names to our list_source codes
                    source_map = {
                        "BIS Entity List": "BIS_ENTITY",
                        "BIS Denied Persons List (DPL)": "BIS_DENIED",
                        "BIS Unverified List (UVL)": "BIS_UVL",
                        "BIS Military End User (MEU) List": "BIS_MEU",
                        "OFAC SDN": "OFAC_SDN",
                        "OFAC Non-SDN Menu-Based Sanctions List (NS-MBS List)": "OFAC_SDN",
                        "OFAC Sectoral Sanctions Identifications List (SSI)": "OFAC_SDN",
                        "OFAC Foreign Sanctions Evaders (FSE) List": "OFAC_SDN",
                        "State AECA Debarred List": "STATE_DEBARRED",
                        "State Nonproliferation Sanctions (ISN)": "STATE_NONPRO",
                    }
                    list_source = source_map.get(source, "CSL_" + source[:10].replace(" ", "_").upper())

                    aliases = entry.get("alt_names", [])
                    if isinstance(aliases, str):
                        aliases = [a.strip() for a in aliases.split(";") if a.strip()]

                    country = ""
                    addresses = entry.get("addresses", [])
                    if addresses and isinstance(addresses, list):
                        country = addresses[0].get("country", "")

                    programs = ", ".join(entry.get("programs", []) or [])
                    source_id = str(entry.get("id", "") or entry.get("source_list_url", ""))

                    entity_type = "INDIVIDUAL" if entry.get("type", "").lower() == "individual" else "ORGANIZATION"

                    self._add_entity(
                        name=name,
                        aliases=aliases,
                        list_source=list_source,
                        entity_type=entity_type,
                        country=country,
                        programs=programs,
                        source_id=source_id,
                    )
                    count += 1

            except Exception as exc:
                logger.error("Error loading US CSL JSON: %s", exc)

            logger.info("Loaded %d entries from US Consolidated Screening List (JSON)", count)
            return

        # --- CSV fallback ---
        if os.path.exists(csv_path):
            count = 0
            try:
                with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        name = (row.get("name") or row.get("Name") or "").strip()
                        if not name:
                            continue
                        source = row.get("source") or row.get("Source") or "CSL"
                        self._add_entity(
                            name=name,
                            aliases=[],
                            list_source="CSL",
                            entity_type="ORGANIZATION",
                            country=row.get("country") or row.get("Country") or "",
                            programs=row.get("programs") or "",
                            source_id=row.get("id") or "",
                        )
                        count += 1
            except Exception as exc:
                logger.error("Error loading US CSL CSV: %s", exc)
            logger.info("Loaded %d entries from US Consolidated Screening List (CSV)", count)
        else:
            logger.info("US CSL not found — run update_lists.py to download it.")

    def _load_opensanctions(self):
        """Load the OpenSanctions FtM bulk dataset.

        Parsing lives in ``opensanctions_client.iter_entities`` so the
        screener just wires the stream into the shared entity store. The
        client handles schema filtering, alias collection, and dedup
        against datasets already covered by our OFAC/UN/UK/EU loaders.
        """
        filepath = os.path.join(self.data_dir, "opensanctions_sanctions.jsonl")
        if not os.path.exists(filepath):
            logger.info("OpenSanctions file not found — run update_lists.py to download it.")
            return

        try:
            from opensanctions_client import iter_entities
        except ImportError as exc:
            logger.warning("opensanctions_client not importable: %s", exc)
            return

        count = 0
        try:
            for entity in iter_entities(filepath):
                self._add_entity(**entity)
                count += 1
        except Exception as exc:
            logger.error("Error loading OpenSanctions data: %s", exc)

        logger.info("Loaded %d entries from OpenSanctions", count)

    # ------------------------------------------------------------------
    # Index building
    # ------------------------------------------------------------------

    def _build_name_index(self):
        """Build a flat list of (normalized_name, entity_index) for matching."""
        self.name_index = []
        for i, entity in enumerate(self.entities):
            norm = self._normalize(entity["name"])
            if norm:
                self.name_index.append((norm, i))
            for alias in entity.get("aliases", []):
                norm = self._normalize(alias)
                if norm:
                    self.name_index.append((norm, i))

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def screen(self, query_name: str, threshold: int = 82, max_results: int = 20) -> List[dict]:
        """
        Match a name against all loaded sanctions lists.
        Returns a list of matches sorted by score (highest first).
        """
        if not query_name or not query_name.strip() or not self.name_index:
            return []

        query_norm = self._normalize(query_name)
        if not query_norm or len(query_norm) < 2:
            return []

        results = []
        seen_indices = set()

        # Use rapidfuzz process.extract for efficient batch matching
        # Extract top candidates from the name index
        names_only = [n for n, _ in self.name_index]

        # token_set_ratio is best for entity names (handles word reordering)
        top_matches = process.extract(
            query_norm,
            names_only,
            scorer=fuzz.token_set_ratio,
            limit=100,
            score_cutoff=threshold - 10,  # Slightly lower cutoff for pre-filtering
        )

        for matched_name_norm, score, idx in top_matches:
            _, entity_idx = self.name_index[idx]
            if entity_idx in seen_indices:
                continue

            # Refine score with multiple algorithms
            entity = self.entities[entity_idx]
            original_name = entity["name"]

            best_score = score
            best_matched = matched_name_norm

            # Check all names/aliases for the best match
            all_names = [original_name] + entity.get("aliases", [])
            for name_variant in all_names:
                name_norm = self._normalize(name_variant)
                if not name_norm:
                    continue
                s = max(
                    fuzz.ratio(query_norm, name_norm),
                    fuzz.token_set_ratio(query_norm, name_norm),
                    fuzz.token_sort_ratio(query_norm, name_norm),
                )
                if s > best_score:
                    best_score = s
                    best_matched = name_variant

            if best_score >= threshold:
                seen_indices.add(entity_idx)
                results.append({
                    "matched_name": best_matched,
                    "listed_name": original_name,
                    "score": round(best_score, 1),
                    "list_source": entity["list_source"],
                    "entity_type": entity["entity_type"],
                    "country": entity.get("country", ""),
                    "programs": entity.get("programs", ""),
                    "source_id": entity.get("source_id", ""),
                    "aliases": entity.get("aliases", [])[:5],  # Limit for display
                })

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:max_results]

    def screen_website(self, website: str, threshold: int = 80) -> List[dict]:
        """
        Screen a website domain against sanctions lists.
        Extracts potential entity names from the domain and searches.
        """
        if not website:
            return []

        # Extract potential name from domain
        # e.g., "acme-trading.com" -> "acme trading"
        domain = website.split("/")[0].split(":")[0]  # Remove path/port
        name_part = domain.rsplit(".", 1)[0] if "." in domain else domain  # Remove TLD
        name_part = name_part.replace("-", " ").replace("_", " ").replace(".", " ")

        if len(name_part) < 3:
            return []

        return self.screen(name_part, threshold=threshold)

    def get_stats(self) -> dict:
        """Return summary statistics about loaded lists."""
        source_counts: Dict[str, int] = {}
        for ent in self.entities:
            src = ent["list_source"]
            source_counts[src] = source_counts.get(src, 0) + 1

        return {
            "total_entities": len(self.entities),
            "total_searchable_names": len(self.name_index),
            "lists_loaded": source_counts,
            "last_updated": self._last_updated,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(s: str) -> str:
        """Normalize a name for matching."""
        import re
        if not s:
            return ""
        s = s.lower().strip()
        s = s.replace("&", " and ")
        s = re.sub(r"[^\w\s]", " ", s)  # Remove punctuation
        s = re.sub(r"\s+", " ", s).strip()
        return s

    @staticmethod
    def _xml_text(elem, tag: str) -> str:
        """Safely extract text from an XML element."""
        child = elem.find(tag)
        if child is not None and child.text:
            return child.text.strip()
        return ""

    # ------------------------------------------------------------------
    # Sanctions page URL resolution
    # ------------------------------------------------------------------

    # Direct URL patterns (no API call needed)
    _DIRECT_URL_PATTERNS = {
        "OFAC_SDN": "https://sanctionssearch.ofac.treas.gov/Details.aspx?id={source_id}",
        "OFAC_CONS": "https://sanctionssearch.ofac.treas.gov/Details.aspx?id={source_id}",
    }

    # Search URL patterns (pre-filled search, no API call needed)
    _SEARCH_URL_PATTERNS = {
        "OFSI_UK": "https://search-uk-sanctions-list.service.gov.uk/?search={entity_name}",
    }

    # Google search templates for lists without direct URLs
    _GOOGLE_SEARCH_TEMPLATES = {
        "EU": '"{entity_name}" site:sanctionsmap.eu OR site:eur-lex.europa.eu sanctions',
        "UN_SC": '"{entity_name}" site:un.org sanctions consolidated list',
        "BIS": '"{entity_name}" site:bis.gov entity list OR denied persons',
        "BIS_ENTITY": '"{entity_name}" site:bis.gov entity list',
        "BIS_DENIED": '"{entity_name}" site:bis.gov denied persons',
        "BIS_UVL": '"{entity_name}" site:bis.gov unverified list',
        "BIS_MEU": '"{entity_name}" site:bis.gov military end user',
    }

    def resolve_sanctions_url(self, match: dict, api_key: str = "", cse_id: str = "") -> Optional[str]:
        """
        Resolve the official sanctions page URL for a match.

        1. Try direct URL construction (OFAC)
        2. Try search URL construction (UK)
        3. Fall back to Google Custom Search API (EU, UN, BIS)

        Returns the URL string or None.
        """
        list_source = match.get("list_source", "")
        source_id = match.get("source_id", "")
        entity_name = match.get("listed_name", "")

        # 1. Direct URL (OFAC)
        if list_source in self._DIRECT_URL_PATTERNS and source_id:
            url = self._DIRECT_URL_PATTERNS[list_source].format(source_id=source_id)
            logger.debug("Direct URL for %s/%s: %s", list_source, source_id, url)
            return url

        # 2. Search URL (UK)
        if list_source in self._SEARCH_URL_PATTERNS and entity_name:
            import urllib.parse
            encoded = urllib.parse.quote(entity_name)
            url = self._SEARCH_URL_PATTERNS[list_source].format(entity_name=encoded)
            logger.debug("Search URL for %s/%s: %s", list_source, entity_name, url)
            return url

        # 3. Google Custom Search API fallback
        if list_source in self._GOOGLE_SEARCH_TEMPLATES and entity_name and api_key and cse_id:
            return self._google_search_url(list_source, entity_name, api_key, cse_id)

        return None

    def _google_search_url(
        self, list_source: str, entity_name: str, api_key: str, cse_id: str
    ) -> Optional[str]:
        """Use Google Custom Search API to find the official sanctions page for an entity."""
        import requests as _requests
        import time

        template = self._GOOGLE_SEARCH_TEMPLATES.get(list_source, "")
        if not template:
            return None

        query = template.format(entity_name=entity_name)

        try:
            r = _requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"q": query, "key": api_key, "cx": cse_id, "num": 3},
                timeout=10,
            )
            time.sleep(0.5)  # Rate limiting

            if r.status_code != 200:
                logger.warning("Google search failed (status %d) for: %s", r.status_code, query[:80])
                return None

            data = r.json()
            items = data.get("items", [])
            if items:
                url = items[0].get("link", "")
                logger.info("Google search found URL for %s [%s]: %s", entity_name[:40], list_source, url[:80])
                return url

        except Exception as exc:
            logger.warning("Google search error for %s: %s", entity_name[:40], exc)

        return None

    def resolve_urls_for_matches(
        self, matches: List[dict], api_key: str = "", cse_id: str = "", min_score: int = 90
    ) -> List[dict]:
        """
        Resolve sanctions page URLs for high-confidence matches.
        Adds a 'sanctions_url' field to each qualifying match dict (in-place).
        Returns the same list for convenience.
        """
        for match in matches:
            if match.get("score", 0) < min_score:
                continue

            url = self.resolve_sanctions_url(match, api_key=api_key, cse_id=cse_id)
            if url:
                match["sanctions_url"] = url

        return matches
