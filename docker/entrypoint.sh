#!/bin/sh
# Container entrypoint: make sure the sample archive exists before serving.
#
# The dataset is mounted from the host for review.  If the mounted directory is
# empty we generate the deterministic sample into it; an existing catalogue is
# never overwritten (the generator would, so it is only run when invited).
set -eu

DATA_DIR="${PITR_DATA_DIR:-/data}"

if [ ! -f "$DATA_DIR/timelines.json" ]; then
    if [ "${PITR_AUTOGENERATE:-1}" = "1" ]; then
        echo "entrypoint: no catalogue in $DATA_DIR; generating sample archive" >&2
        python /app/tools/make_sample.py --out "$DATA_DIR"
    else
        echo "entrypoint: no catalogue in $DATA_DIR and PITR_AUTOGENERATE=0" >&2
        exit 1
    fi
fi

exec "$@"
