#!/bin/sh
# Pick the process from EMPIRE_SERVICE: api | publisher.
set -eu
case "${EMPIRE_SERVICE:-}" in
  api) exec tasks-api ;;
  publisher) exec empire-publish --loop "${PUBLISH_INTERVAL:-5}" ;;
  *) echo "EMPIRE_SERVICE must be 'api' or 'publisher'" >&2; exit 64 ;;
esac
