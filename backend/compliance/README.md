# SanctionSight — Compliance Package

SR 11-7 / EU AI Act validation artefacts. This directory is the regulator-facing evidence pack: model card, governance, validation harness, fairness tests, incident runbooks, and SOC 2 readiness tracker.

## Contents

```
compliance/
├── README.md                 # this file
├── model_card.md             # SR 11-7 model card — conceptual soundness, data lineage, limitations
├── governance.md             # change management, approval gates, RACI
├── soc2_readiness.md         # TSC-mapped control tracker for Q3 2026 audit
├── validation/
│   ├── README.md             # how the harness works and how to extend
│   ├── run.py                # golden-dataset harness — precision / recall / F1 per risk type
│   └── golden_dataset.csv    # seed corpus (target 200 labelled cases)
├── fairness_tests/
│   ├── README.md
│   ├── conftest.py           # sys.path shim for pytest
│   ├── test_name_scripts.py  # per-script match rate floor + gap-from-Latin cap
│   └── test_fp_by_country.py # per-country FP share cap + absolute HIGH-FP rate ceiling
└── runbooks/
    ├── README.md
    ├── false_negative.md
    ├── false_positive.md
    ├── llm_outage.md
    └── list_update_failure.md
```

A `governance_log/` directory will be created on the first Tier A / Tier B change after Phase 6 closes. It is excluded from this initial publication.

## Reading order

For a regulator or external reviewer looking at this for the first time:

1. `model_card.md` — what the system is, what it does, what it does not.
2. `governance.md` — how changes are controlled.
3. `validation/README.md` → `validation/run.py` → `validation/golden_dataset.csv` — how we measure and gate.
4. `fairness_tests/README.md` → the two test files — how we protect against known bias failure modes.
5. `runbooks/` — how we handle incidents.
6. `soc2_readiness.md` — where we are on the TSC map, what's still open.

## Running everything locally

```bash
cd sanctions-tool/backend

pip install -r requirements.txt
python -m spacy download en_core_web_lg
alembic upgrade head

# Validation harness (gate: release acceptance thresholds)
python -m compliance.validation.run --strict --json-out /tmp/validation.json

# Fairness suite
pytest compliance/fairness_tests/ -v

# Unit + integration (pre-existing)
pytest tests/unit/ -v
```

All four must pass for a release. The CI pipeline runs exactly these four steps in this order.

## Versioning

This package is versioned alongside the main tool — version `2.3-phase6` corresponds to the Phase 6 deliverable dated 2026-04-18. Revisions follow the Tier A / Tier B / Tier C rules in `governance.md`.

## Contact

Questions, validation artefacts, auditor access requests: **enquiries@sanctionsight.com**.
