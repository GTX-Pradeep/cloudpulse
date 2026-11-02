-- CloudPulse metadata / evidence store (runs on the META server, never on target).
-- Every table maps to one step of:
--   telemetry -> regression -> plan evidence -> diagnosis -> candidate
--   -> experiment -> raw runs -> verdict
-- Constraints encode the rules the code relies on, so a bug cannot silently
-- write an impossible state (e.g. a verdict label that does not exist).

CREATE EXTENSION IF NOT EXISTS vector;

-- Query identity. One row per pg_stat_statements entry we have seen.
-- queryid is only unique together with dbid/userid/toplevel.
CREATE TABLE IF NOT EXISTS queries (
    id            bigserial PRIMARY KEY,
    queryid       bigint  NOT NULL,
    dbid          oid     NOT NULL,
    userid        oid     NOT NULL,
    toplevel      boolean NOT NULL DEFAULT true,
    query_text    text    NOT NULL,
    template_name text,               -- matching tracked-workload template, if any
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (queryid, dbid, userid, toplevel)
);

-- Raw cumulative counters, exactly as read. Deltas are derived, never stored
-- in place of the raw values, so a bug in delta logic can be fixed and replayed.
CREATE TABLE IF NOT EXISTS stat_snapshots (
    id              bigserial PRIMARY KEY,
    taken_at        timestamptz NOT NULL DEFAULT now(),
    stats_reset     timestamptz,          -- pg_stat_statements_info.stats_reset
    dealloc         bigint,               -- pg_stat_statements_info.dealloc
    postmaster_start timestamptz NOT NULL -- detects server restarts
);

CREATE TABLE IF NOT EXISTS query_stats_snapshots (
    snapshot_id         bigint NOT NULL REFERENCES stat_snapshots(id) ON DELETE CASCADE,
    query_id            bigint NOT NULL REFERENCES queries(id),
    calls               bigint NOT NULL,
    total_exec_time_ms  double precision NOT NULL,
    rows                bigint NOT NULL,
    shared_blks_hit     bigint NOT NULL,
    shared_blks_read    bigint NOT NULL,
    temp_blks_written   bigint NOT NULL,
    PRIMARY KEY (snapshot_id, query_id)
);
CREATE INDEX IF NOT EXISTS query_stats_snapshots_query_idx
    ON query_stats_snapshots (query_id, snapshot_id);

-- Per-interval view derived from consecutive snapshots of the same query.
-- An interval is invalid (no delta) when counters went backwards, the server
-- restarted, or pg_stat_statements was reset in between.
CREATE OR REPLACE VIEW query_intervals AS
SELECT q.query_id,
       s.id                AS snapshot_id,
       s.taken_at,
       prev_s.taken_at     AS prev_taken_at,
       q.calls - p.calls   AS calls,
       q.total_exec_time_ms - p.total_exec_time_ms AS exec_time_ms,
       CASE WHEN q.calls > p.calls
            THEN (q.total_exec_time_ms - p.total_exec_time_ms) / (q.calls - p.calls) END
                           AS mean_ms,
       q.shared_blks_read - p.shared_blks_read     AS blks_read,
       q.temp_blks_written - p.temp_blks_written   AS temp_written,
       (q.calls < p.calls
        OR s.postmaster_start <> prev_s.postmaster_start
        OR s.stats_reset IS DISTINCT FROM prev_s.stats_reset) AS invalid
FROM query_stats_snapshots q
JOIN stat_snapshots s ON s.id = q.snapshot_id
JOIN LATERAL (
    SELECT p.* FROM query_stats_snapshots p
    WHERE p.query_id = q.query_id AND p.snapshot_id < q.snapshot_id
    ORDER BY p.snapshot_id DESC LIMIT 1
) p ON true
JOIN stat_snapshots prev_s ON prev_s.id = p.snapshot_id;

CREATE TABLE IF NOT EXISTS performance_incidents (
    id              bigserial PRIMARY KEY,
    query_id        bigint NOT NULL REFERENCES queries(id),
    detected_at     timestamptz NOT NULL DEFAULT now(),
    detector        text NOT NULL,
    baseline_median_ms double precision NOT NULL,
    baseline_mad_ms    double precision NOT NULL,
    current_ms         double precision NOT NULL,
    ratio              double precision NOT NULL,
    evidence        jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'investigating', 'resolved', 'no_action', 'failed')),
    outcome         text,
    scenario        text            -- set by the evaluation harness only
);
-- at most one open incident per query
CREATE UNIQUE INDEX IF NOT EXISTS one_open_incident_per_query
    ON performance_incidents (query_id) WHERE status IN ('open', 'investigating');

CREATE TABLE IF NOT EXISTS execution_plans (
    id            bigserial PRIMARY KEY,
    query_id      bigint NOT NULL REFERENCES queries(id),
    incident_id   bigint REFERENCES performance_incidents(id) ON DELETE CASCADE,
    captured_at   timestamptz NOT NULL DEFAULT now(),
    source        text NOT NULL CHECK (source IN ('baseline_explain', 'incident_explain_analyze', 'lab')),
    params        jsonb,
    fingerprint   text NOT NULL,
    plan          jsonb NOT NULL,
    findings      jsonb NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS execution_plans_query_idx ON execution_plans (query_id, captured_at);

CREATE TABLE IF NOT EXISTS diagnoses (
    id           bigserial PRIMARY KEY,
    incident_id  bigint NOT NULL REFERENCES performance_incidents(id) ON DELETE CASCADE,
    rule         text NOT NULL,
    hypothesis   text NOT NULL,
    evidence     jsonb NOT NULL,
    limitations  text NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id            bigserial PRIMARY KEY,
    incident_id   bigint NOT NULL REFERENCES performance_incidents(id) ON DELETE CASCADE,
    diagnosis_id  bigint REFERENCES diagnoses(id) ON DELETE CASCADE,
    kind          text NOT NULL CHECK (kind IN ('index', 'analyze', 'setting')),
    description   text NOT NULL,
    setup_sql     jsonb NOT NULL DEFAULT '[]',
    session       jsonb NOT NULL DEFAULT '{}',
    hypopg        jsonb,          -- what-if result, null if not applicable
    status        text NOT NULL DEFAULT 'proposed'
                  CHECK (status IN ('proposed', 'filtered_out', 'queued', 'tested', 'failed')),
    UNIQUE (incident_id, description)
);

CREATE TABLE IF NOT EXISTS experiments (
    id            bigserial PRIMARY KEY,
    candidate_id  bigint NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    protocol      jsonb NOT NULL,
    environment   jsonb,
    run_dir       text,
    status        text NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'done', 'failed')),
    error         text
);

-- Every timed execution. This is the raw evidence behind every verdict.
CREATE TABLE IF NOT EXISTS experiment_runs (
    experiment_id bigint NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    query_name    text NOT NULL,
    block         int  NOT NULL CHECK (block >= 0),
    arm           char(1) NOT NULL CHECK (arm IN ('A', 'B')),
    position      smallint NOT NULL,
    seq           int NOT NULL,
    param_index   int NOT NULL,
    latency_us    double precision,
    status        text NOT NULL CHECK (status IN ('ok', 'timeout')),
    rows          bigint,
    PRIMARY KEY (experiment_id, query_name, block, arm, seq)
);

CREATE TABLE IF NOT EXISTS verdicts (
    id            bigserial PRIMARY KEY,
    experiment_id bigint NOT NULL UNIQUE REFERENCES experiments(id) ON DELETE CASCADE,
    label         text NOT NULL CHECK (label IN ('SUPPORTED', 'REJECTED', 'INCONCLUSIVE')),
    reason        text NOT NULL,
    target        jsonb NOT NULL,
    guardrail     jsonb NOT NULL,
    decided_at    timestamptz NOT NULL DEFAULT now()
);

-- Work queue. Workers claim rows with FOR UPDATE SKIP LOCKED and hold a lease;
-- a crashed worker's lease expires and the job becomes claimable again.
CREATE TABLE IF NOT EXISTS jobs (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('investigate', 'experiment')),
    payload      jsonb NOT NULL,
    status       text NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts     int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 3,
    locked_by    text,
    lease_until  timestamptz,
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_claimable_idx ON jobs (created_at) WHERE status IN ('queued', 'running');

-- Structural vectors for similarity search (secondary feature).
CREATE TABLE IF NOT EXISTS query_embeddings (
    incident_id   bigint PRIMARY KEY REFERENCES performance_incidents(id) ON DELETE CASCADE,
    repr_version  int NOT NULL,
    features      jsonb NOT NULL,
    embedding     vector(32) NOT NULL
);

CREATE OR REPLACE VIEW incident_overview AS
SELECT i.id, i.detected_at, i.status, i.outcome, i.ratio, i.scenario,
       q.template_name, q.query_text,
       (SELECT count(*) FROM candidates c WHERE c.incident_id = i.id) AS n_candidates,
       (SELECT string_agg(v.label, ',' ORDER BY v.id)
          FROM verdicts v JOIN experiments e ON e.id = v.experiment_id
          JOIN candidates c ON c.id = e.candidate_id WHERE c.incident_id = i.id) AS verdicts
FROM performance_incidents i JOIN queries q ON q.id = i.query_id;
