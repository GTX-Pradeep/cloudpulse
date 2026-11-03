"""Command-line interface.

    cloudpulse init-meta                       create the evidence database schema
    cloudpulse bench experiments/x.json        run one A/B (or A/A) experiment
    cloudpulse calibrate                       repeated A/A runs to check the method
    cloudpulse evaluate [--scenario s1 ...]    run the ground-truth scenario suite
    cloudpulse worker                          run jobs from the queue forever
    cloudpulse serve                           start the web UI / API
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from pathlib import Path

from cloudpulse.config import load_settings


def cmd_init_meta(args) -> None:
    from cloudpulse.meta import store
    store.init_schema(load_settings().meta_dsn, reset=args.reset)
    print("meta schema ready")


def cmd_bench(args) -> None:
    from cloudpulse.bench.harness import run_experiment
    from cloudpulse.bench.spec import ExperimentSpec
    from cloudpulse.workload import guardrail

    spec = ExperimentSpec.load(args.spec)
    if args.blocks:
        spec.protocol.blocks = args.blocks
    res = run_experiment(spec, load_settings(), rebuild_base=args.rebuild)
    target = next(q for q in spec.queries if q.name == (spec.target_query or spec.queries[0].name))
    others = [q for q in spec.queries if q is not target]
    rep = guardrail.evaluate(res.rows, target, others)
    (res.run_dir / "verdict.json").write_text(json.dumps(rep.to_dict(), indent=2))
    print(json.dumps({"run_dir": str(res.run_dir), "label": rep.label, "reason": rep.reason,
                      "collateral": rep.collateral}, indent=2))


def cmd_calibrate(args) -> None:
    from cloudpulse.calibration import run_calibration
    out = run_calibration(load_settings(), repeats=args.repeats, blocks=args.blocks, scale=args.scale)
    print(f"calibration written to {out}")


def cmd_evaluate(args) -> None:
    from cloudpulse.evaluation import run_all
    out = run_all(load_settings(), args.scenario, args.scale, args.seed,
                  baseline_rounds=args.baseline_rounds, fault_rounds=args.fault_rounds,
                  reset_meta=not args.keep_meta, label=args.label, pack=args.pack)
    print((out / "summary.md").read_text())
    print(f"results in {out}")


def cmd_similarity(args) -> None:
    import json as _json
    from datetime import datetime

    from cloudpulse.config import REPO_ROOT
    from cloudpulse.similarity import benchmark
    res = [benchmark.run(k) for k in (1, 2, 4)]
    out = REPO_ROOT / "results" / "similarity"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (out / f"{stamp}_benchmark.json").write_text(_json.dumps(res, indent=2))
    print(_json.dumps(res, indent=2))


def cmd_replicate(args) -> None:
    from cloudpulse.replication import replicate
    out = replicate(load_settings(), repeats=args.repeats)
    print((out / "summary.md").read_text())


def cmd_worker(args) -> None:  # pragma: no cover
    from cloudpulse.jobs.worker import run_forever
    run_forever(load_settings())


def cmd_serve(args) -> None:  # pragma: no cover
    import uvicorn
    uvicorn.run("cloudpulse.api.app:app", host=args.host, port=args.port)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="cloudpulse")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init-meta")
    s.add_argument("--reset", action="store_true")
    s.set_defaults(fn=cmd_init_meta)

    s = sub.add_parser("bench")
    s.add_argument("spec", type=Path)
    s.add_argument("--blocks", type=int)
    s.add_argument("--rebuild", action="store_true", help="rebuild the lab base database")
    s.set_defaults(fn=cmd_bench)

    s = sub.add_parser("calibrate")
    s.add_argument("--repeats", type=int, default=10)
    s.add_argument("--blocks", type=int, default=12)
    s.add_argument("--scale", default="default")
    s.set_defaults(fn=cmd_calibrate)

    s = sub.add_parser("evaluate")
    s.add_argument("--scenario", action="append")
    s.add_argument("--scale", default="default")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--baseline-rounds", type=int, default=14)
    s.add_argument("--fault-rounds", type=int, default=8)
    s.add_argument("--keep-meta", action="store_true",
                   help="append to the evidence database instead of resetting it")
    s.add_argument("--label", help="suffix for the results directory (e.g. supplementary)")
    s.add_argument("--pack", default="ecommerce", choices=["ecommerce", "tpch"],
                   help="workload pack: synthetic e-commerce or the TPC-H benchmark")
    s.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("similarity-benchmark")
    s.set_defaults(fn=cmd_similarity)

    s = sub.add_parser("replicate", help="re-run tested candidates to check verdict stability")
    s.add_argument("--repeats", type=int, default=3)
    s.set_defaults(fn=cmd_replicate)

    s = sub.add_parser("worker")
    s.set_defaults(fn=cmd_worker)

    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
