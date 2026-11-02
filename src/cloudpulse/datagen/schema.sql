-- Synthetic e-commerce schema used for every CloudPulse experiment.
-- Deliberately minimal: two tables are enough to produce selective filters,
-- skewed joins, top-N queries, aggregation spills and update-heavy traffic.

CREATE TABLE customers (
    id              bigint      PRIMARY KEY,
    name            text        NOT NULL,
    country         text        NOT NULL,
    segment         text        NOT NULL,
    signup_at       timestamptz NOT NULL,
    -- end of the customer's activity window; customers "churn" at different times
    last_active_at  timestamptz NOT NULL,
    CHECK (last_active_at >= signup_at)
);

-- fillfactor 90 leaves free space on each page so that updates which do not
-- touch an indexed column can be HOT (heap-only tuple) updates. Without it,
-- full pages would make HOT impossible and hide the cost of extra indexes.
CREATE TABLE orders (
    id          bigint        PRIMARY KEY,
    customer_id bigint        NOT NULL,
    status      text          NOT NULL,
    amount      numeric(10,2) NOT NULL CHECK (amount >= 0),
    created_at  timestamptz   NOT NULL,
    updated_at  timestamptz   NOT NULL,
    CHECK (updated_at >= created_at)
) WITH (fillfactor = 90);
