#!/usr/bin/env bash
# Reproduce every CloudPulse result on your own machine.
#
#   ./scripts/reproduce.sh            full run (about 3-5 hours on a laptop)
#   ./scripts/reproduce.sh --quick    smaller dataset and fewer scenarios (~15 min)
#
# Needs: Docker (with compose v2), Python 3.11+, git. On Windows, run it from WSL2.
# Close heavy applications first: the benchmarks measure your machine.
set -euo pipefail
cd "$(dirname "$0")/.."

QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

command -v docker >/dev/null || { echo "Docker is required"; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose v2 is required"; exit 1; }

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi
# Label results with this machine unless the user chose a label already.
if ! grep -q '^CP_ENV_LABEL=official' .env; then
  sed -i.bak "s/^CP_ENV_LABEL=.*/CP_ENV_LABEL=official-$(hostname -s)/" .env && rm -f .env.bak
fi

echo "==> Starting PostgreSQL servers (target, lab, meta)"
docker compose up -d --build --wait

echo "==> Installing CloudPulse into .venv"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -e ".[dev]"

echo "==> Tests"
pytest -q

echo "==> Similarity retrieval benchmark"
cloudpulse similarity-benchmark

if [[ $QUICK == 1 ]]; then
  echo "==> A/A calibration (quick)"
  cloudpulse calibrate --repeats 3 --blocks 10 --scale small
  echo "==> Ground-truth evaluation (quick: small dataset, 3 scenarios)"
  cloudpulse evaluate --scale small --scenario s0_control \
    --scenario s1_dropped_fk_index --scenario s5_created_at_index_dropped
else
  echo "==> A/A calibration"
  cloudpulse calibrate --repeats 10 --blocks 12 --scale default
  echo "==> Ground-truth evaluation (all scenarios)"
  cloudpulse evaluate --scale default
  echo "==> Replication of every tested candidate (3 extra runs each)"
  cloudpulse replicate --repeats 3
  echo "==> TPC-H validation (scale factor 1)"
  cloudpulse evaluate --pack tpch --scale tpch-sf1 --keep-meta --label tpch-sf1
fi

echo
echo "Done. Results:"
echo "  results/evaluation/<timestamp>/summary.md"
echo "  results/calibration/<timestamp>/summary.json"
echo "  results/runs/   (every timed execution, per experiment)"
echo "Browse the evidence:  .venv/bin/cloudpulse serve   ->  http://127.0.0.1:8000"
