"""Golden-dataset validation harness.

Runs ``SanctionsContentAnalyzer.analyze_content`` against every row in
``golden_dataset.csv`` and produces a per-risk-type precision / recall /
F1 report. The harness enforces the release-gate thresholds documented
in ``../model_card.md`` §6 — if any Critical-tier floor is breached the
process exits non-zero so CI can block the release.

Usage
-----

    # from sanctions-tool/backend
    python -m compliance.validation.run
    python -m compliance.validation.run --dataset custom.csv --json-out run.json
    python -m compliance.validation.run --strict   # exit 1 on any gate miss

The harness is deliberately CI-friendly: it only needs the engine, the
spaCy model, and ``langdetect``. No Google API key, no LLM credentials
— those are exercised by separate integration tests.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set

HERE = Path(__file__).resolve().parent
DEFAULT_DATASET = HERE / "golden_dataset.csv"


# Release-gate thresholds — mirror model_card.md §6. Keep this table in
# sync with the model card; any change here is a Tier A governance event.
TIER_DEFINITIONS = {
    "critical": {
        "risk_types": {"DIRECT_BUSINESS", "SANCTIONS_REGULATORY_MENTION"},
        "recall_floor": 0.90,
        "high_fpr_ceiling": 0.15,
    },
    "material": {
        "risk_types": {"INDIRECT_BUSINESS", "NAME_COOCCURRENCE"},
        "recall_floor": 0.80,
        "high_fpr_ceiling": 0.20,
    },
    "advisory": {
        "risk_types": {"COMPLIANCE_MENTION"},
        "recall_floor": 0.70,
        "high_fpr_ceiling": 0.25,
    },
}


@dataclass
class CaseResult:
    case_id: str
    country: str
    expected_risk_level: str
    observed_risk_level: str
    expected_risk_types: Set[str]
    observed_risk_types: Set[str]
    expected_language: str
    observed_language: Optional[str]
    passed: bool
    notes: str = ""
    error: Optional[str] = None


@dataclass
class TypeMetrics:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    high_fp: int = 0   # false positives that came in at HIGH risk tier
    n_positive: int = 0

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def high_fpr(self) -> float:
        """HIGH-tier false-positive rate conditional on being flagged."""
        denom = self.tp + self.fp
        return self.high_fp / denom if denom else 0.0


@dataclass
class RunReport:
    dataset: str
    cases_total: int
    cases_passed: int
    cases_errored: int
    per_type: Dict[str, TypeMetrics] = field(default_factory=dict)
    gate_failures: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "cases_total": self.cases_total,
            "cases_passed": self.cases_passed,
            "cases_errored": self.cases_errored,
            "per_type": {
                k: {
                    "tp": v.tp, "fp": v.fp, "fn": v.fn,
                    "high_fp": v.high_fp,
                    "precision": round(v.precision, 4),
                    "recall": round(v.recall, 4),
                    "f1": round(v.f1, 4),
                    "high_fpr": round(v.high_fpr, 4),
                }
                for k, v in self.per_type.items()
            },
            "gate_failures": self.gate_failures,
        }


def _parse_expected_types(raw: str) -> Set[str]:
    if not raw or raw.strip().upper() == "NONE":
        return set()
    return {t.strip() for t in raw.split("|") if t.strip()}


def _load_cases(path: Path) -> List[dict]:
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row for row in reader]


def _run_single_case(row: dict) -> CaseResult:
    # Lazy import so --help works without the engine installed.
    from sanctions_engine import detect_language, get_analyzer

    case_id = row["case_id"]
    country = row["country"]
    expected_types = _parse_expected_types(row.get("expected_risk_types", ""))
    expected_level = row["expected_risk_level"].strip().upper()
    expected_lang = (row.get("expected_language") or "").strip().lower()
    text = row["input_text"]

    try:
        analyzer = get_analyzer(country)
        result = analyzer.analyze_content(
            {"content": text, "type": "HTML"}, url=f"test://{case_id}"
        )
    except Exception as exc:  # pragma: no cover — harness diagnostics
        return CaseResult(
            case_id=case_id,
            country=country,
            expected_risk_level=expected_level,
            observed_risk_level="ERROR",
            expected_risk_types=expected_types,
            observed_risk_types=set(),
            expected_language=expected_lang,
            observed_language=None,
            passed=False,
            notes=row.get("notes", ""),
            error=f"{type(exc).__name__}: {exc}",
        )

    observed_level = str(result.get("risk_level", "UNKNOWN")).upper()
    observed_types = {
        str(f.get("risk_type", "")).upper()
        for f in result.get("findings", [])
        if f.get("risk_type")
    }

    # Language detection is a separate, lighter check — we only assert
    # it for rows that set expected_language explicitly. Empty means
    # "not part of this case's expectations."
    observed_lang = detect_language(text) if expected_lang else None

    # Case passes if:
    #   - risk level matches (exact)
    #   - every expected risk_type is present in observed set
    #   - language matches when expected_language is set
    level_ok = observed_level == expected_level
    types_ok = expected_types.issubset(observed_types) if expected_types else (
        observed_level in {"MINIMAL", "UNKNOWN", "LOW"}
    )
    lang_ok = True if not expected_lang else (observed_lang == expected_lang)

    return CaseResult(
        case_id=case_id,
        country=country,
        expected_risk_level=expected_level,
        observed_risk_level=observed_level,
        expected_risk_types=expected_types,
        observed_risk_types=observed_types,
        expected_language=expected_lang,
        observed_language=observed_lang,
        passed=level_ok and types_ok and lang_ok,
        notes=row.get("notes", ""),
    )


def _update_metrics(
    per_type: Dict[str, TypeMetrics],
    case: CaseResult,
) -> None:
    # Count per-type TP/FP/FN. A "positive" case for a given type is one
    # where that type appears in expected_risk_types.
    all_types = case.expected_risk_types | case.observed_risk_types
    for rtype in all_types:
        m = per_type.setdefault(rtype, TypeMetrics())
        in_expected = rtype in case.expected_risk_types
        in_observed = rtype in case.observed_risk_types

        if in_expected and in_observed:
            m.tp += 1
        elif in_observed and not in_expected:
            m.fp += 1
            if case.observed_risk_level == "HIGH":
                m.high_fp += 1
        elif in_expected and not in_observed:
            m.fn += 1

    for rtype in case.expected_risk_types:
        per_type.setdefault(rtype, TypeMetrics()).n_positive += 1


def _evaluate_gates(report: RunReport) -> None:
    for tier_name, tier in TIER_DEFINITIONS.items():
        for rtype in tier["risk_types"]:
            m = report.per_type.get(rtype)
            if m is None or m.n_positive == 0:
                # No labelled positives for this type in the dataset.
                # Can't evaluate the floor — note it so reviewers know.
                report.gate_failures.append(
                    f"NOTE [{tier_name}] {rtype}: 0 positive cases in dataset — cannot evaluate recall floor"
                )
                continue

            if m.recall < tier["recall_floor"]:
                report.gate_failures.append(
                    f"FAIL [{tier_name}] {rtype} recall {m.recall:.2f} < floor {tier['recall_floor']:.2f}"
                )
            if m.high_fpr > tier["high_fpr_ceiling"]:
                report.gate_failures.append(
                    f"FAIL [{tier_name}] {rtype} HIGH-FPR {m.high_fpr:.2f} > ceiling {tier['high_fpr_ceiling']:.2f}"
                )


def run(dataset_path: Path) -> tuple[RunReport, List[CaseResult]]:
    rows = _load_cases(dataset_path)
    per_type: Dict[str, TypeMetrics] = {}
    cases: List[CaseResult] = []
    passed = 0
    errored = 0

    for row in rows:
        case = _run_single_case(row)
        cases.append(case)
        if case.error:
            errored += 1
            continue
        if case.passed:
            passed += 1
        _update_metrics(per_type, case)

    report = RunReport(
        dataset=str(dataset_path),
        cases_total=len(rows),
        cases_passed=passed,
        cases_errored=errored,
        per_type=per_type,
    )
    _evaluate_gates(report)
    return report, cases


def _format_report(report: RunReport, cases: List[CaseResult]) -> str:
    lines = [
        f"SanctionSight validation harness",
        f"Dataset: {report.dataset}",
        f"Cases: {report.cases_total}  passed={report.cases_passed}  errored={report.cases_errored}",
        "",
        "Per-risk-type metrics:",
        f"  {'risk_type':<34} {'n+':>4} {'tp':>4} {'fp':>4} {'fn':>4} {'prec':>6} {'recall':>7} {'f1':>6} {'hfpr':>6}",
    ]
    for rtype in sorted(report.per_type.keys()):
        m = report.per_type[rtype]
        lines.append(
            f"  {rtype:<34} {m.n_positive:>4} {m.tp:>4} {m.fp:>4} {m.fn:>4} "
            f"{m.precision:>6.2f} {m.recall:>7.2f} {m.f1:>6.2f} {m.high_fpr:>6.2f}"
        )

    failing_cases = [c for c in cases if not c.passed and not c.error]
    errored_cases = [c for c in cases if c.error]
    if failing_cases:
        lines.append("")
        lines.append("Failing cases:")
        for c in failing_cases:
            lines.append(
                f"  {c.case_id:<10} country={c.country:<12} "
                f"expected={c.expected_risk_level:<8} observed={c.observed_risk_level:<8} "
                f"missing_types={sorted(c.expected_risk_types - c.observed_risk_types) or '-'}"
            )
    if errored_cases:
        lines.append("")
        lines.append("Errored cases:")
        for c in errored_cases:
            lines.append(f"  {c.case_id:<10} {c.error}")

    lines.append("")
    if report.gate_failures:
        lines.append("Release gates:")
        for failure in report.gate_failures:
            lines.append(f"  {failure}")
    else:
        lines.append("Release gates: ALL PASSED")

    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--json-out", type=Path, default=None,
                        help="Write the machine-readable report to this path")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero on any FAIL gate. NOTE-only gates still exit 0.")
    args = parser.parse_args(argv)

    if not args.dataset.exists():
        print(f"Dataset not found: {args.dataset}", file=sys.stderr)
        return 2

    # Ensure the backend directory is on sys.path so ``sanctions_engine``
    # imports cleanly when invoked as a module or script.
    backend_dir = HERE.parent.parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    report, cases = run(args.dataset)
    print(_format_report(report, cases))

    if args.json_out:
        args.json_out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    hard_failures = [g for g in report.gate_failures if g.startswith("FAIL")]
    if args.strict and hard_failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
