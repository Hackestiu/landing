#!/bin/sh
# Seeds the models volume with the files baked into the image (knowledge sheets,
# vision labels.json), then starts the app. -n never clobbers, so a file already
# in the volume wins and the weights bootstrap.py downloaded are left alone.
set -e

if [ -d /opt/seed-models ]; then
    cp -rn /opt/seed-models/. /app/models/ 2>/dev/null || true
fi

exec python app.py
