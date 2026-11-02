# 5 · System design: storage, jobs, API, similarity

**Code:** `meta/schema.sql`, `jobs/queue.py`, `jobs/worker.py`, `pipeline.py`, `api/`, `similarity/`

## The evidence database
One table per step of the workflow:
`queries → stat_snapshots / query_stats_snapshots → performance_incidents →
execution_plans → diagnoses → candidates → experiments → experiment_runs → verdicts`.

DBMS features are used where they solve a real problem:
- **CHECK constraints** on every status and label, so a bug cannot write a state that doesn't exist.
- **Partial unique index** `one_open_incident_per_query`: at most one open incident
  per query, enforced by the database even with concurrent detectors.
- **A view** (`query_intervals`) computes deltas with a `LATERAL` join to the
  previous snapshot. Raw counters stay untouched, so a bug in the delta logic can
  be fixed and replayed.
- **COPY** bulk-loads thousands of raw timings per experiment.
- **`ON DELETE CASCADE`** keeps the evidence tree consistent when an investigation is retried.

## The job queue
```sql
UPDATE jobs SET status='running', locked_by=$worker, attempts=attempts+1,
       lease_until = now() + interval '120 s'
WHERE id = (SELECT id FROM jobs
            WHERE attempts < max_attempts
              AND (status='queued' OR (status='running' AND lease_until < now()))
            ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, kind, payload;
```
- `SKIP LOCKED`: concurrent workers skip rows another worker has locked instead
  of waiting, so each worker gets a different job (tested with 4 threads).
- **Lease:** a running worker renews it every 30 s. If the worker crashes, the
  lease expires and another worker reclaims the job, up to `max_attempts`. The
  old worker's heartbeat then fails, because the job is no longer its own.
- **Idempotent jobs:** an investigation deletes its previous diagnoses and
  candidates first; an experiment rebuilds its twins. Retrying is safe.

## API and UI
FastAPI serves server-rendered pages (incident list, incident detail with
regression chart, plan tree, diagnoses, candidates, per-block ratio strip,
guardrail table, similar incidents) and a JSON API (`/api/incidents`,
`/api/experiments/{id}/runs`, `POST /api/incidents/{id}/investigate`, `/api/jobs`).

## Similarity (secondary)
A 32-dimensional deterministic vector per incident: SQL structure from the real
PostgreSQL parser (pglast), counts of plan node types, and finding flags.
log1p-scaled and L2-normalised, stored as a pgvector `vector(32)`, with exact
cosine search. It is shown as context ("what was tested last time on
something shaped like this"), never as a diagnosis.

## Interview questions
1. **Why a PostgreSQL queue instead of Kafka or Redis?**
   A few long jobs; we need durability, transactions with the evidence we write,
   and crash recovery. PostgreSQL does all of that with no new system to run.
2. **What happens if a worker crashes mid-experiment?**
   Its lease expires, another worker reclaims the job, the twins are rebuilt from
   scratch, and the attempt counter goes up. After `max_attempts` the job is
   marked failed with its last error.
3. **How do you prevent duplicate incidents?**
   A partial unique index on `(query_id) WHERE status IN ('open','investigating')`.
   The second insert fails and is ignored.
4. **Why store raw timings in the database and in CSV?**
   The database serves the UI and queries; the CSV plus `meta.json` is a
   self-contained, citable artefact for reproducibility.
5. **Why is pgvector exact search fine here?**
   At this scale a sequential scan over a few thousand 32-dimensional vectors
   takes milliseconds and returns exact results. An ANN index trades recall for
   speed we don't need.
