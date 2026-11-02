# Results

Every number in the top-level README comes from a file in this folder. All
results here were produced in the development cloud sandbox (2 vCPU, PostgreSQL 16)
and are labelled `dev-cloud-sandbox (not official)`. Run `scripts/reproduce.sh` to
produce results on your machine; they are labelled `official-<hostname>`.

| Path | What it is |
|---|---|
| `evaluation/20261102T185926Z/` | **Main ground-truth evaluation** (all 7 scenarios, corrected code) |
| `evaluation/20261102T191456Z_supplementary-16-rounds/` | S2 re-run with a longer post-fault window (16 intervals) |
| `evaluation/20261102T191912Z_supplementary-after-F9/` | S5 re-run after the heavy-hitter fix (F-9) |
| `evaluation/20261102T184027Z/` | First run, **superseded** (bugs F-7/F-8; see its NOTE.md) |
| `calibration/20261102T185543Z/` | A/A calibration: 10 repeats × 5 queries, identical arms |
| `similarity/` | Structural-vector retrieval benchmark |
| `runs/<timestamp>_<name>/` | One directory per experiment: `raw.csv` (every timed execution), `meta.json` (environment, settings, dataset fingerprint, arms, parameters), `plans.jsonl` (plan chosen per arm and parameter), `verdict.json` |

Each `summary.json` records the git commit of the code that produced it. A
`-dirty` suffix means the working tree had uncommitted files at the time,
which during these runs were the result files being written.
