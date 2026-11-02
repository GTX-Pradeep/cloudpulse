"""Web UI and JSON API over the evidence database.

Server-rendered HTML (FastAPI + Jinja2): the UI's job is to make the evidence
readable, not to be the technical core, so there is no separate frontend build.
"""

from __future__ import annotations

import math
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from cloudpulse.config import load_settings
from cloudpulse.jobs import queue
from cloudpulse.meta import store
from cloudpulse.plans import tree

app = FastAPI(title="CloudPulse", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _meta() -> str:
    return load_settings().meta_dsn


# --------------------------------------------------------------------------- data access


def list_incidents(meta: str) -> list[dict]:
    return store.fetch_all(meta, "SELECT * FROM incident_overview ORDER BY id DESC")


def incident_detail(meta: str, incident_id: int) -> dict:
    inc = store.fetch_one(
        meta,
        "SELECT i.*, q.template_name, q.query_text, q.queryid FROM performance_incidents i "
        "JOIN queries q ON q.id = i.query_id WHERE i.id = %s", (incident_id,))
    if inc is None:
        raise HTTPException(404, "incident not found")
    inc["plan"] = store.fetch_one(
        meta, "SELECT plan, findings, params, fingerprint FROM execution_plans "
              "WHERE incident_id = %s AND source = 'incident_explain_analyze'", (incident_id,))
    inc["diagnoses"] = store.fetch_all(
        meta, "SELECT * FROM diagnoses WHERE incident_id = %s ORDER BY id", (incident_id,))
    inc["candidates"] = store.fetch_all(
        meta,
        "SELECT c.*, e.id AS experiment_id, e.status AS experiment_status, e.run_dir, e.error, "
        "e.environment->>'env_label' AS env_label, v.label, v.reason, v.target, v.guardrail "
        "FROM candidates c LEFT JOIN experiments e ON e.candidate_id = c.id "
        "LEFT JOIN verdicts v ON v.experiment_id = e.id WHERE c.incident_id = %s ORDER BY c.id",
        (incident_id,))
    try:
        from cloudpulse.similarity.search import similar
        inc["similar"] = similar(meta, incident_id, 5)
    except Exception:  # similarity is optional; never break the page
        inc["similar"] = []
    return inc


# --------------------------------------------------------------------------- view helpers


def sparkline(values: list[float | None], width: int = 1000, height: int = 140) -> dict:
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return {"path": "", "width": width, "height": height, "max": 0, "points": []}
    vmax = max(v for _, v in pts) * 1.1 or 1
    n = len(values) - 1 or 1
    coords = [(round(8 + i / n * (width - 16), 1), round(height - 8 - v / vmax * (height - 24), 1))
              for i, v in pts]
    return {"path": "M" + " L".join(f"{x},{y}" for x, y in coords), "width": width,
            "height": height, "max": vmax, "points": coords}


def ratio_strip(effect: dict | None, width: int = 1000) -> dict | None:
    """Log-scale strip of per-block ratios with the CI band and the 1.0 line."""
    if not effect or not effect.get("block_ratios"):
        return None
    vals = effect["block_ratios"] + [effect["ci_low"], effect["ci_high"], 1.0]
    lo = math.log(min(vals)) - 0.15
    hi = math.log(max(vals)) + 0.15

    def x(v: float) -> float:
        return round(10 + (math.log(v) - lo) / (hi - lo) * (width - 20), 1)

    return {"width": width, "dots": [x(v) for v in effect["block_ratios"]],
            "ci": (x(effect["ci_low"]), x(effect["ci_high"])), "one": x(1.0),
            "est": x(effect["ratio"]), "lo_label": round(math.exp(lo), 3),
            "hi_label": round(math.exp(hi), 3)}


def plan_rows(plan_json) -> list[dict]:
    if not plan_json:
        return []
    p = tree.parse(plan_json)
    out: list[dict] = []

    def walk(n: tree.PlanNode, depth: int) -> None:
        out.append({
            "depth": depth, "node": n.node_type, "relation": n.relation, "index": n.index_name,
            "est": n.plan_rows, "act": n.actual_rows, "loops": n.actual_loops,
            "q_error": n.q_error, "self_ms": n.self_time_ms, "filter": n.filter or n.index_cond,
            "removed": n.rows_removed_by_filter, "spilled": n.spilled,
        })
        for c in n.children:
            walk(c, depth + 1)

    walk(p.root, 0)
    total = p.execution_ms or 0
    for r in out:
        r["share"] = (r["self_ms"] or 0) / total if total else 0
    return out


templates.env.globals.update(sparkline=sparkline, ratio_strip=ratio_strip, plan_rows=plan_rows)
templates.env.filters["pct"] = lambda r: "—" if r is None else f"{(r - 1) * 100:+.1f}%"
templates.env.filters["int_"] = lambda v: "—" if v is None else f"{v:,.0f}"
templates.env.filters["num"] = lambda v: "—" if v is None else (
    f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}")


# --------------------------------------------------------------------------- routes


@app.get("/healthz")
def healthz() -> dict:
    store.fetch_one(_meta(), "SELECT 1 AS ok")
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"incidents": list_incidents(_meta())})


@app.get("/incidents/{incident_id}", response_class=HTMLResponse)
def incident_page(request: Request, incident_id: int):
    return templates.TemplateResponse(request, "incident.html",
                                      {"i": incident_detail(_meta(), incident_id)})


@app.get("/api/incidents")
def api_incidents() -> list[dict]:
    return list_incidents(_meta())


@app.get("/api/incidents/{incident_id}")
def api_incident(incident_id: int) -> dict:
    return incident_detail(_meta(), incident_id)


@app.get("/api/experiments/{experiment_id}/runs")
def api_runs(experiment_id: int) -> list[dict]:
    return store.fetch_all(
        _meta(), "SELECT query_name, block, arm, position, seq, param_index, latency_us, status, rows "
                 "FROM experiment_runs WHERE experiment_id = %s ORDER BY query_name, block, position, seq",
        (experiment_id,))


class InvestigateRequest(BaseModel):
    lab_state: dict


@app.post("/api/incidents/{incident_id}/investigate", status_code=202)
def api_investigate(incident_id: int, body: InvestigateRequest) -> dict:
    if store.fetch_one(_meta(), "SELECT id FROM performance_incidents WHERE id = %s", (incident_id,)) is None:
        raise HTTPException(404, "incident not found")
    job = queue.enqueue(_meta(), "investigate", {"incident_id": incident_id, "lab_state": body.lab_state})
    return {"job_id": job, "status": "queued"}


@app.get("/api/jobs")
def api_jobs() -> list[dict]:
    return store.fetch_all(_meta(), "SELECT id, kind, status, attempts, locked_by, lease_until, "
                                    "last_error, created_at, updated_at FROM jobs ORDER BY id DESC LIMIT 100")
