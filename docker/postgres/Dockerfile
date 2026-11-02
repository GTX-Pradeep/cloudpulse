# One image for all three CloudPulse servers (target, lab, meta).
# The base image is pinned to an exact minor version so results stay reproducible.
# Bump it deliberately and record the change in docs/decisions.md.
ARG PG_IMAGE=postgres:17.6-bookworm
FROM ${PG_IMAGE}

# The official image already has the PostgreSQL apt repository (PGDG) configured.
#  - hypopg:   hypothetical indexes for cheap what-if planning (lab server)
#  - pgvector: structural similarity search over past incidents (meta server)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-17-hypopg \
      postgresql-17-pgvector \
 && rm -rf /var/lib/apt/lists/*
