#!/bin/sh
# yt-dlp and gallery-dl break whenever a platform changes, so the server refreshes
# both on every start. Offline starts keep the installed versions.
if [ "$1" = "uvicorn" ]; then
    pip install --no-cache-dir --quiet --upgrade --root-user-action=ignore \
        --disable-pip-version-check "yt-dlp[default]" gallery-dl \
        || echo "warning: could not update yt-dlp/gallery-dl; using installed versions" >&2
fi
exec "$@"
