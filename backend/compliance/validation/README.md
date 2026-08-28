# Validation Harness

Golden-dataset validation for the SanctionSight engine. Produces the
per-risk-type precision / recall / F1 numbers reported in the model card
and enforces the release-gate thresholds used by CI.

## What this is

`run.py` loads `golden_dataset.csv`, runs every row through
`SanctionsContentAnalyzer.analyze_content`, and compares the observed
risk tier + finding set against the labelled expectations. The harness
is intentionally network-free — no Google CSE, no LLM — so it can run
on a laptop or in CI without credentials.

The LLM brief generator, citation verifier, and full pipeline (with
Google retrieval) are exercised by separate integration tests under
`backend/tests/integration/`. This harness is scoped to the decision
core: the scorer that tells an analyst whether a given piece of text is
DIRECT_BUSINESS, INDIRECT_BUSINESS, COMPLIANCE_MENTION, etc.

## Running

```bash
cd sanctions-tool/backend

# Prereqs
pip install -r requirements.txt
python -m spacy download en_core_web_lg

# Full report to stdout
python -m compliance.validation.run

# CI-style run: non-zero exit on any FAIL gate
python -m compliance.validation.run --strict

# Emit machine-readable JSON alongside the stdout report
python -m compliance.validation.run --json-out /tmp/validation.json

# Point at a different dataset (e.g. a customer's labelled set)
python -m compliance.validation.run --dataset custom_dataset.csv --strict
```

## Dataset schema

`golden_dataset.csv` — RFC 4180 CSV with header row.

| Column | Required | Values | Notes |
|---|---|---|---|
| `case_id` | yes | string, unique | Stable id for the case (e.g. `GDS-001`) |
| `country` | yes | one of `SANCTIONED_ENTITIES` | Which analyzer context to instantiate |
| `target_entity` | yes | free text | Human-readable label; informational |
| `input_text` | yes | free text | What the analyzer sees. Quote if it contains commas |
| `expected_risk_level` | yes | `HIGH` / `MEDIUM` / `LOW` / `MINIMAL` / `UNKNOWN` | Exact match required |
| `expected_risk_types` | yes | pipe-separated or `NONE` | Subset check — all listed types must appear among findings |
| `expected_language` | no | ISO-639-1 code (`en`, `ru`, …) or blank | When set, `detect_language(text)` must equal this code |
| `notes` | no | free text | For human reviewers; ignored by the harness |

## Case-pass rules

A case passes when **all** of the following hold:

1. `observed_risk_level == expected_risk_level`.
2. Every type in `expected_risk_types` appears in the analyzer's findings (subset check, not exact equality — additional observed types are allowed and do not fail the case).
3. If `expected_risk_types` is `NONE`, the observed risk level must be in `{MINIMAL, UNKNOWN, LOW}`. This is the shape of the negative / false-positive-trap cases.
4. If `expected_language` is set, `detect_language(input_text)` returns that code.

## Release gates

Mirrors `model_card.md` §6. Thresholds live in `run.py::TIER_DEFINITIONS`
— editing them is a Tier A governance event (see `governance.md`).

| Tier | Risk types | Recall floor | HIGH-FPR ceiling |
|---|---|---|---|
| Critical | `DIRECT_BUSINESS`, `SANCTIONS_REGULATORY_MENTION` | ≥ 0.90 | ≤ 0.15 |
| Material | `INDIRECT_BUSINESS`, `NAME_COOCCURRENCE` | ≥ 0.80 | ≤ 0.20 |
| Advisory | `COMPLIANCE_MENTION` | ≥ 0.70 | ≤ 0.25 |

`--strict` exits with status 1 on any `FAIL` gate. `NOTE`-only gates
(risk types the dataset has no labelled positives for) do not fail the
run — they indicate the dataset needs more coverage for that type.

## Extending the dataset

Target: 200 labelled cases before the first external validation review.
When adding cases:

- Prefer real-world-shaped text over synthetic phrasings. Paraphrase
  public news articles; do not paste copyrighted text verbatim.
- **Do not use real identified business names** unless the sanctions
  linkage is a matter of public record (enforcement action, court
  filing, OFAC designation). When in doubt, fictionalise the entity
  and keep the typology.
- Preserve per-script balance. Target roughly: 60% Latin, 15% Cyrillic,
  10% Arabic, 10% CJK, 5% other. The fairness tests enforce this.
- Every added row is a Tier A governance change. Log it in
  `backend/compliance/governance_log/`.

## Regenerating the baseline

When a Tier A change ships (prompt / threshold / keyword update), the
harness output **is** the new baseline. Re-run, attach the JSON to the
governance log entry, and include a diff against the prior baseline
highlighting any per-type metric movement ≥ 0.02.

## What this harness does NOT cover

- Google-retrieval quality (covered by `backend/tests/integration/`)
- LLM brief generation (covered by `backend/tests/integration/`)
- Evidence-packet contents (covered by `backend/tests/unit/test_evidence_packet.py`)
- Fairness beyond what the dataset exercises (covered by `../fairness_tests/`)
- UI copy (covered by `backend/tests/unit/test_copy_audit.py`)

Each of those has its own runner; the release gate is the union of all
five passing.
