#!/bin/sh
# Pick the process from EMPIRE_SERVICE: api | publisher | mcp.
set -eu
case "${EMPIRE_SERVICE:-}" in
  api) exec tasks-api ;;
  publisher) exec empire-publish --loop "${PUBLISH_INTERVAL:-5}" ;;
  mcp) exec empire-mcp-http ;;
  *) echo "EMPIRE_SERVICE must be 'api', 'publisher', or 'mcp'" >&2; exit 64 ;;
esac
