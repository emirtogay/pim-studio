#!/bin/sh
# Container entrypoint: bring the schema up to head, then hand off to uvicorn.
# `exec` makes uvicorn PID 1 so `docker stop` delivers SIGTERM to it directly,
# which lets the FastAPI shutdown hooks finalise any in-flight work cleanly.
set -e

alembic upgrade head

# Then the curated registry. `seed.run()` only ever adds what is missing, so it
# is safe on every boot — and running it here is what makes a mapping change
# ship with the image that contains it. Left out until 2026-08-25, which meant
# `attribute_columns` only changed when someone remembered to run the seed by
# hand on the server: the per-market description mappings shipped in 0.20.0 and
# did nothing, because the rows naming them were never inserted.
#
# Fatal on failure, like the migration above. A panel serving against a
# half-seeded registry reports real content as missing, and this project would
# rather not boot than report a gap that is not there.
python -m pim_studio.seed

# The What's New page fetches `demo/changelog.json` as a static file. In
# production `static/demo/` is a bind mount from the host, so anything the
# image shipped at that path is masked — and on a fresh host the directory is
# empty. Regenerate on every boot from the CHANGELOG.md baked into this image,
# so the page always matches the version actually running. Never fatal: a
# missing release-notes page is not a reason to refuse to serve the panel.
DEMO_OUT_DIR="${DEMO_OUT_DIR:-/app/src/pim_studio/admin/static/demo}" \
    python -m pim_studio.changelog >/dev/null 2>&1 \
    || echo "warning: could not write the changelog payload; /whats-new will be empty"

# Demo mode: every external system is a mock (`pim_studio/integrations.py`) and
# the database is filled from the synthetic catalogue, through the sync's own
# stages. Safe on every boot — on a database it already seeded the demo seed
# only regenerates the static files, which a rebuilt container has lost. On a
# database holding anything else it refuses, and like the steps above that
# stops the container: a demo must never be laid over real data.
case "${DEMO_MODE:-}" in
    1|true|True|TRUE|yes|Yes|YES|on|On|ON)
        python -m pim_studio.demo.seed --skip-migrations \
            --out "${DEMO_OUT_DIR:-/app/src/pim_studio/admin/static/demo}"
        ;;
esac

# The port is a variable because production runs this container on the host's
# network namespace, where there is no `-p 8003:8000` to translate and 8000 is
# already another application's. Defaults to 8000 so a bridged run — local
# development, and the compose healthcheck that goes with it — is unchanged.
exec uvicorn pim_studio.admin.app:app \
    --host 0.0.0.0 \
    --port "${APP_PORT:-8000}" \
    --proxy-headers \
    --forwarded-allow-ips='*'
