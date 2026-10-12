import asyncio

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from empire_mcp.server import build_server

SECRET = "postgres://user:hunter2@db.internal/secret-row-42"


def server_with(status: int, body: object) -> object:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    client = httpx.Client(base_url="http://tasks.test", transport=transport)
    return build_server(client, None, lambda: "test-token")


@pytest.mark.parametrize("body", [
    {"detail": {"reason": SECRET}},
    {"detail": {"error": {"nested": SECRET}}},
    {"detail": SECRET},
    {"message": SECRET},
])
def test_tool_errors_never_carry_upstream_text(body):
    with pytest.raises(ToolError) as failure:
        asyncio.run(server_with(403, body).call_tool("tasks.list", {}))
    assert str(failure.value) == "403: forbidden" or SECRET not in str(failure.value)
    assert SECRET not in str(failure.value)
    assert "hunter2" not in repr(failure.value)


def test_server_errors_map_to_fixed_words():
    with pytest.raises(ToolError) as failure:
        asyncio.run(server_with(503, {"detail": {"reason": SECRET}}).call_tool("tasks.list", {}))
    assert "503: upstream_error" in str(failure.value)
