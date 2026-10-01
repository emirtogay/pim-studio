# syntax=docker/dockerfile:1.7

# ─── Frontend build stage ───────────────────────────────────────────────────
# Vite is configured with outDir='../static', so the build output lands at
# /repo/src/pim_studio/admin/static/ — the source path is kept intact in this
# stage so that relative path resolves correctly.
FROM node:20-alpine AS frontend
WORKDIR /repo/src/pim_studio/admin/frontend
COPY src/pim_studio/admin/frontend/package*.json ./
RUN npm ci
COPY src/pim_studio/admin/frontend/ ./
RUN npm run build
# Output now at /repo/src/pim_studio/admin/static/

# ─── Python runtime stage ───────────────────────────────────────────────────
# Plain slim Python: no scrapers and no headless browser here. 3.12 matches
# pyproject's floor.
FROM python:3.12-slim AS app
WORKDIR /app

# curl is only here for the compose healthcheck against /api/health.
#
# `pg_dump` is deliberately NOT installed here. It was, briefly, from PGDG —
# the client must not be older than the server and bookworm ships 15 against
# a server on 18 — and that layer failed the image build, which blocks every
# deploy rather than just the snapshot job. A backup that cannot be built is
# worth less than one that fails loudly at runtime, which is what
# `pim_studio.snapshots` now does: it reports that pg_dump is absent and says
# where it comes from. Restore this only with a build that has been proven.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY CHANGELOG.md ./
# Only the .py: the scheduled sync runs build_demo_data_from_db.py inside a
# container of this image, and data files have no business in it.
COPY scripts/*.py ./scripts/

RUN pip install --no-cache-dir -e .

# Drop the freshly built SPA into the spot FastAPI mounts as static.
COPY --from=frontend /repo/src/pim_studio/admin/static ./src/pim_studio/admin/static

# Mount points for gitignored / volatile state.
RUN mkdir -p /app/logs /app/data/uploads

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
