# Fairness Tests

Regression-style pytest suite that protects against well-known
sanctions-screening failure modes:

- **Script bias** — matcher works well on Latin names but silently
  degrades on Cyrillic / Arabic / Chinese-pinyin inputs
  (`test_name_scripts.py`).
- **Country bias** — a single jurisdiction's cultural vocabulary
  dominates the false-positive pool (`test_fp_by_country.py`).

## Running

```bash
cd sanctions-tool/backend
pytest compliance/fairness_tests/ -v
```

`test_name_scripts.py` runs without any heavy engine deps beyond
`rapidfuzz` and skips cleanly if it's missing.

`test_fp_by_country.py` needs spaCy and the full engine; it uses
`importorskip("spacy")` so CI environments without the model fall
through rather than failing.

## Thresholds

| Test | Control | Threshold |
|---|---|---|
| Per-script match rate | Non-Latin script above its own floor | Latin 1.00 / Cyrillic 0.75 / Arabic 0.66 / CJK 0.66 |
| Gap from Latin | Non-Latin ≤ 0.35 below Latin rate | 0.35 |
| Per-country FP share | Single country contribution to HIGH FPs | ≤ 0.30 |
| Overall HIGH FP rate | Across all negative probes | ≤ 0.20 |

Editing any of these thresholds is a Tier A governance event per
`../governance.md` §4.

## Extending

New probes welcome — both scripts and countries. When adding:

1. Source the FP text from a real-world edge case the support team has
   seen (not a speculative one).
2. Keep the probe *minimal* — one phenomenon per probe so a failing
   assertion points at exactly one issue.
3. If the probe requires a new jurisdiction in `SANCTIONED_ENTITIES`,
   that is a Tier A change; land it separately first.
4. Add a matching entry to the validation golden dataset if the FP is
   novel enough that the validation harness should lock it in too.

## Historic incidents this suite was designed to prevent

- **2026-Q1 "Cuban coffee" regression** — a single blog roll-up page
  mentioning Cuban coffee eight times produced HIGH-tier findings that
  saturated an analyst queue. Root cause: no per-country FP cap. This
  suite's `test_no_single_country_dominates_high_fps` is the durable
  fix.
- **Cyrillic name-match gap** — an early build used `rapidfuzz.ratio`
  (character-level) rather than `token_set_ratio` (tokenised), causing
  Cyrillic names to under-match because there were no spaces between
  prefix and surname in some source formats. Fixed by the tokeniser
  switch; `test_per_script_match_rate_above_floor` prevents regression.
