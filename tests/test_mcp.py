import json

import pytest
from starlette.testclient import TestClient

import config
from app import create_app, models
from app import mcp_server
from mcp.server.mcpserver.exceptions import ToolError


def test_tools_directly(today):
    created = mcp_server.create_task("Call Sam", due_date="tomorrow", priority="high")
    assert created["source"] == "claude" and created["due"] == "tomorrow" and created["card_queued"]
    listed = mcp_server.list_tasks()
    assert listed["count"] == 1
    updated = mcp_server.update_task(created["id"], status="doing", clear_due_date=True, reprint=True)
    assert updated["status"] == "doing" and updated["due_date"] is None
    assert mcp_server.complete_task(created["id"])["status"] == "done"
    assert mcp_server.list_tasks()["count"] == 0
    assert mcp_server.list_tasks(status="done")["count"] == 1
    assert mcp_server.print_task(created["id"])["queued"]
    assert mcp_server.print_daily_digest()["queued"]
    with pytest.raises(ToolError, match="no task #42"):
        mcp_server.complete_task(42)
    with pytest.raises(ToolError, match="due date"):
        mcp_server.create_task("x", due_date="whenever")


HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(client, path, method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    return client.post(path, content=json.dumps(body), headers=HEADERS)


def test_http_endpoint_behind_secret(monkeypatch):
    monkeypatch.setattr(config, "MCP_SECRET", "s3cret")
    app = mcp_server.build_asgi_app(create_app())
    with TestClient(app) as client:
        path = "/mcp/s3cret"
        init = rpc(client, path, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}})
        assert init.status_code == 200, init.text
        assert init.json()["result"]["serverInfo"]["name"] == "Corkboard"
        tools = rpc(client, path, "tools/list", id_=2).json()["result"]["tools"]
        assert {t["name"] for t in tools} == {
            "create_task", "list_tasks", "update_task", "complete_task", "print_task", "print_daily_digest"}
        call = rpc(client, path, "tools/call", {"name": "create_task", "arguments": {"title": "Via HTTP"}}, id_=3)
        assert call.status_code == 200 and not call.json()["result"].get("isError")
        assert models.list_tasks()[0]["title"] == "Via HTTP"
        assert client.post("/mcp/wrong", content="{}", headers=HEADERS).status_code == 404
        assert client.get("/").status_code == 200  # Flask still serves the board


def test_connector_disabled_without_secret(monkeypatch):
    monkeypatch.setattr(config, "MCP_SECRET", "")
    with TestClient(mcp_server.build_asgi_app(create_app())) as client:
        assert rpc(client, "/mcp/", "initialize").status_code == 404
