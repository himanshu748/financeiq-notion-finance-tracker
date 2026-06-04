from __future__ import annotations

from types import SimpleNamespace

import pytest

import main


class FakeServerParameters:
    def __init__(self, *, command: str, args: list[str], env: dict[str, str]) -> None:
        self.command = command
        self.args = args
        self.env = env


class FakeClientSession:
    def __init__(self, read: object, write: object) -> None:
        self.initialized = False

    async def __aenter__(self) -> "FakeClientSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def initialize(self) -> None:
        self.initialized = True

    async def call_tool(self, tool: str, args: dict) -> SimpleNamespace:
        assert self.initialized is True
        assert tool == "API-get-self"
        assert args == {}
        return SimpleNamespace(
            content=[SimpleNamespace(text='{"id":"notion-user","name":"FinanceIQ"}')]
        )


class FakeStdioClient:
    def __init__(self, params: FakeServerParameters) -> None:
        self.params = params

    async def __aenter__(self) -> tuple[object, object]:
        assert self.params.command == "npx"
        assert self.params.args == ["-y", "@notionhq/notion-mcp-server"]
        assert self.params.env["NOTION_TOKEN"] == "ntn_test"
        return object(), object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class FakeAsyncClient:
    instances: list["FakeAsyncClient"] = []

    def __init__(self, *, timeout: int) -> None:
        self.timeout = timeout
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []
        self.instances.append(self)

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(
        self, url: str, *, headers: dict, params: dict | None = None
    ) -> FakeResponse:
        self.calls.append(("get", url, params, None))
        return FakeResponse({"ok": True})

    async def post(
        self, url: str, *, headers: dict, json: dict | None = None
    ) -> FakeResponse:
        self.calls.append(("post", url, None, json))
        return FakeResponse({"ok": True})

    async def patch(
        self, url: str, *, headers: dict, json: dict | None = None
    ) -> FakeResponse:
        self.calls.append(("patch", url, None, json))
        return FakeResponse({"ok": True})


@pytest.mark.asyncio
async def test_notion_mcp_uses_official_stdio_server(monkeypatch):
    monkeypatch.setattr(main, "NOTION_TOKEN", "ntn_test")
    monkeypatch.setattr(main, "StdioServerParameters", FakeServerParameters)
    monkeypatch.setattr(main, "ClientSession", FakeClientSession)
    monkeypatch.setattr(main, "stdio_client", lambda params: FakeStdioClient(params))

    async with main.notion_session() as session:
        result = await main.mcp_call(session, "API-get-self", {})

    assert result == {"id": "notion-user", "name": "FinanceIQ"}
    assert main.notion_transport_name() == "mcp-stdio"


@pytest.mark.asyncio
async def test_notion_mcp_requires_token(monkeypatch):
    monkeypatch.setattr(main, "NOTION_TOKEN", "")

    with pytest.raises(main.HTTPException, match="NOTION_TOKEN"):
        async with main.notion_session():
            pass


@pytest.mark.asyncio
async def test_rest_fallback_does_not_mutate_tool_arguments(monkeypatch):
    FakeAsyncClient.instances = []
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    fallback = main.NotionHTTPFallback()
    args = {"database_id": "database_123", "filter": {"property": "Month"}}

    result = await fallback.call_tool("API-post-database-query", args)

    assert result == {"ok": True}
    assert args == {"database_id": "database_123", "filter": {"property": "Month"}}
    client = FakeAsyncClient.instances[0]
    assert client.timeout == 30
    assert client.calls == [
        (
            "post",
            f"{main.NOTION_API}/databases/database_123/query",
            None,
            {"filter": {"property": "Month"}},
        )
    ]
