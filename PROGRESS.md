# Progress

_Last updated: 2026-11-03_

## Status: v0.1 complete (all milestones M1–M9 implemented)

| Milestone | State | Where |
|---|---|---|
| M1 Measurement bench | done | `datagen/`, `bench/` |
| M2 Statistical verifier + A/A calibration | done | `stats/`, `calibration.py` |
| M3 Workload guardrail + collateral scenarios | done | `workload/` |
| M4 Plan analyzer + golden tests | done | `plans/`, `tests/golden/` |
| M5 Diagnosis, candidates, HypoPG filter | done | `diagnosis/` |
| M6 Collector + detector + scenario evaluation | done | `telemetry/`, `evaluation.py` |
| M7 Job queue, worker, API | done | `jobs/`, `pipeline.py`, `api/` |
| M8 UI + pgvector similarity | done | `api/templates/`, `similarity/` |
| M9 Reproduce script, README, CI | done | `scripts/reproduce.sh`, `.github/workflows/ci.yml` |

## Results so far (dev cloud sandbox, PostgreSQL 16, labelled "not official")
- Corrected evaluation: `results/evaluation/20261102T185926Z`. Supplementary,
  labelled runs: `…_supplementary-16-rounds` (S2) and `…_supplementary-after-F9` (S5).
- Superseded first run kept for the record: `results/evaluation/20261102T184027Z` (see NOTE.md there).
- A/A calibration: `results/calibration/20261102T185543Z`; similarity: `results/similarity/`.
- Headline: S5's `created_at` index is REJECTED for a 5.66× collateral regression
  (replicated by `experiments/ab_created_at_index_trap.json`: 5.21×).
- CI (GitHub Actions) runs all tests on the pinned PostgreSQL 17 image.

## Next steps
1. **Run `./scripts/reproduce.sh` on your own laptop** to produce official
   results on PostgreSQL 17, then replace the dev numbers in the README with yours.
2. Read `docs/learn/01…05` and answer the interview questions without looking.
3. Possible extensions, only if they earn their place:
   - repeat whole experiments to fix the A/A interval under-coverage (F-5);
   - generic-plan regressions (prepared statements) as a scenario and a rule;
   - extended statistics (`CREATE STATISTICS`) as a candidate for `estimation_limit`;
   - snapshot-based lab provisioning (`pg_dump`/restore) instead of deterministic rebuild;
   - auto_explain log ingestion for plans captured at the moment of slowness.

## Open questions
- Should the guardrail veto be configurable per query (e.g. ignore reports run once a day)?
- What margin should the decision rule use on a quieter machine? (Calibrate from A/A.)

## Known issues
- A/A confidence intervals under-cover on a noisy VM (see `docs/decisions.md` F-5).
- Detector works on interval means; a change in parameter mix can look like a regression.
- Scenario timing is round-based (time-compressed), not wall-clock.
