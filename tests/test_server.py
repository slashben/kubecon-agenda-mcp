import json
from pathlib import Path

import pytest
from mcp.server.transport_security import TransportSecuritySettings
from httpx import ASGITransport, AsyncClient

from kubecon_agenda_mcp.feed import AgendaStore
from kubecon_agenda_mcp.server import build_server
from tests.test_agenda import CONFIG

HERE = Path(__file__).parent
MAIN = json.loads((HERE / "fixture_feed.json").read_text())
COLOCATED = json.loads((HERE / "fixture_colocated.json").read_text())


@pytest.fixture
def store():
    s = AgendaStore(CONFIG)
    s.load_static((MAIN, "Main conference"), (COLOCATED, "Co-located events"))
    return s


@pytest.mark.asyncio
async def test_tools_registered(store):
    mcp = build_server(CONFIG, store)
    names = {t.name for t in await mcp.list_tools()}
    assert names == {"describe_event", "search_sessions", "get_session", "whats_on", "find_speaker", "check_agenda"}


@pytest.mark.asyncio
async def test_search_tool_rejects_bad_time(store):
    mcp = build_server(CONFIG, store)
    result = await mcp.call_tool("search_sessions", {"after": "25:99"})
    assert "Bad filter" in result[0][0].text if isinstance(result, tuple) else "Bad filter" in str(result)


@pytest.mark.asyncio
async def test_search_tool_finds_session(store):
    mcp = build_server(CONFIG, store)
    result = await mcp.call_tool("search_sessions", {"speaker": "Hirschberg"})
    text = str(result)
    assert "1236746" in text


@pytest.mark.asyncio
async def test_whats_on_groups_blocks(store):
    mcp = build_server(CONFIG, store)
    text = str(await mcp.call_tool("whats_on", {"at": "mon 9:20"}))
    assert "Talks in progress" in text and "All-day blocks in progress" in text
    assert "Agentics Day: MCP + Agents Hosted by CNCF" in text


@pytest.mark.asyncio
async def test_http_endpoints(store):
    mcp = build_server(CONFIG, store)
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/health")
            assert r.status_code == 200 and r.json()["ok"]
            r = await c.get("/")
            assert "MCP endpoint" in r.text
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
            }
            headers = {"Accept": "application/json, text/event-stream"}
            r = await c.post("/mcp", json=init, headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["result"]["serverInfo"]["name"] == "kubecon-agenda"
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers)
            assert r.status_code == 200, r.text
            assert any(t["name"] == "check_agenda" for t in r.json()["result"]["tools"])
