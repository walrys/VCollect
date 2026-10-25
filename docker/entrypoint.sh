#!/bin/sh
# yt-dlp and instaloader break whenever a platform changes, so the server refreshes
# both on every start. Offline starts keep the installed versions.
if [ "$1" = "uvicorn" ]; then
    pip install --no-cache-dir --quiet --upgrade --root-user-action=ignore \
        --disable-pip-version-check "yt-dlp[default]" instaloader \
        || echo "warning: could not update yt-dlp/instaloader; using installed versions" >&2
fi
exec "$@"
