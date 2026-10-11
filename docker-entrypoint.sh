#!/bin/sh
# Pick the process from EMPIRE_SERVICE: api | publisher | mcp | recurring.
set -eu
case "${EMPIRE_SERVICE:-}" in
  api) exec tasks-api ;;
  publisher) exec empire-publish --loop "${PUBLISH_INTERVAL:-5}" ;;
  mcp) exec empire-mcp-http ;;
  recurring) exec empire-recurring ;;
  *) echo "EMPIRE_SERVICE must be 'api', 'publisher', 'mcp', or 'recurring'" >&2; exit 64 ;;
esac
