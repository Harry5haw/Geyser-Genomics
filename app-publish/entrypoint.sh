#!/bin/sh
# The publisher renders pages and signs nothing (no sessions, no forms on the static site), but Django refuses to
# start with DEBUG off and no secret key. A random one per run, rather than a fixed key baked into a public image.
set -eu
if [ -z "${DJANGO_SECRET_KEY:-}" ]; then
    DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(50))')"
    export DJANGO_SECRET_KEY
fi
exec "$@"
