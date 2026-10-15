import asyncio
import json
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import jwt
import pytest
from ag_ui.core import EventType
from azure.cosmos.aio import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from workplace_agent.ui.auth import Authenticator, Principal
from workplace_agent.ui.config import UIConfig
from workplace_agent.ui.events import ResponseEvents
from workplace_agent.ui.foundry import FoundryGateway, preview_reference
from workplace_agent.ui.models import Reference, RunRecord, ToolActivity
from workplace_agent.ui.payloads import payload_text
from workplace_agent.ui.references import references_from_text, sharepoint_file, validate_preview_url
from workplace_agent.ui.server import Services, create_app, with_heartbeats
from workplace_agent.ui.store import SessionStore, now

TENANT = "11111111-1111-4111-8111-111111111111"
API = "22222222-2222-4222-8222-222222222222"
BROWSER = "33333333-3333-4333-8333-333333333333"
ALICE = "44444444-4444-4444-8444-444444444444"
BOB = "55555555-5555-4555-8555-555555555555"


@pytest.fixture
def config(tmp_path: Path) -> UIConfig:
    return UIConfig(
        tenant_id=TENANT, api_client_id=API, browser_client_id=BROWSER,
        identity_client_id="66666666-6666-4666-8666-666666666666",
        origin="https://ui.example.com",
        project_endpoint="https://example.services.ai.azure.com/api/projects/workplace",
        agent_name="workplace-productivity",
        cosmos_endpoint="https://example.documents.azure.com",
        cosmos_database="workplace", cosmos_container="uiSessions", static_directory=tmp_path,
    )


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def user_token(config: UIConfig, signing_key, **changed) -> str:
    claims = {
        "exp": int(time.time()) + 300, "iat": int(time.time()),
        "iss": f"https://login.microsoftonline.com/{config.tenant_id}/v2.0",
        "aud": config.api_client_id, "tid": config.tenant_id, "oid": ALICE,
        "azp": config.browser_client_id, "scp": "access_as_user",
    }
    claims.update(changed)
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "ui-test"})


def authenticator(config: UIConfig, signing_key) -> Authenticator:
    auth = Authenticator(config)
    auth.keys = MagicMock(get_signing_key_from_jwt=MagicMock(
        return_value=SimpleNamespace(key=signing_key.public_key()),
    ))
    return auth


@pytest.mark.parametrize("changed", [
    {"aud": BROWSER}, {"tid": BOB}, {"azp": BOB}, {"scp": "Files.Read"},
    {"oid": "not-an-object-id"}, {"exp": 1}, {"iss": "https://evil.example.com"},
])
def test_ui_rejects_wrong_audience_tenant_caller_scope_and_expired_users(
    config: UIConfig, signing_key, changed: dict,
) -> None:
    with pytest.raises(HTTPException) as error:
        authenticator(config, signing_key).validate(user_token(config, signing_key, **changed))
    assert error.value.status_code == 401


def test_ui_validates_signature_and_partitions_by_trusted_user(config: UIConfig, signing_key) -> None:
    auth = authenticator(config, signing_key)
    token = user_token(config, signing_key)
    principal = auth.validate(token)
    assert principal.object_id == ALICE
    assert token not in repr(principal)
    assert principal.owner(config) != Principal(TENANT, BOB, token).owner(config)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(HTTPException):
        auth.validate(user_token(config, other_key))


@pytest.fixture
def memory_store() -> SessionStore:
    documents: dict[tuple[str, str], dict] = {}
    counter = 0
    container = MagicMock(spec=ContainerProxy)

    def save(body: dict) -> dict:
        nonlocal counter
        counter += 1
        result = {**deepcopy(body), "_etag": str(counter)}
        documents[(body["owner"], body["id"])] = result
        return deepcopy(result)

    async def read(item: str, partition_key: str) -> dict:
        document = documents.get((partition_key, item))
        if document is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return deepcopy(document)

    async def replace(item: str, body: dict, *, etag: str, **kwargs) -> dict:
        current = documents[(body["owner"], item)]
        if current["_etag"] != etag:
            raise CosmosHttpResponseError(status_code=412, message="changed")
        return save(body)

    def query(query: str, *, partition_key: str, parameters=None):
        async def values():
            wanted = "session" if "c.kind = 'session'" in query else "run"
            session = parameters[0]["value"] if parameters else None
            for (owner, _), document in list(documents.items()):
                if owner == partition_key and document["kind"] == wanted:
                    if session is None or document["session_id"] == session:
                        yield deepcopy(document)
        return values()

    async def delete(item: str, partition_key: str) -> None:
        del documents[(partition_key, item)]

    container.create_item = AsyncMock(side_effect=save)
    container.upsert_item = AsyncMock(side_effect=save)
    container.read_item = AsyncMock(side_effect=read)
    container.replace_item = AsyncMock(side_effect=replace)
    container.delete_item = AsyncMock(side_effect=delete)
    container.query_items.side_effect = query
    return SessionStore(container)


async def test_session_catalog_history_and_trace_cannot_cross_users(memory_store: SessionStore) -> None:
    alice = await memory_store.create("alice", "conv_alice")
    await memory_store.create("bob", "conv_bob")
    assert [session["id"] for session in await memory_store.list("alice")] == [alice["session_id"]]
    with pytest.raises(HTTPException) as error:
        await memory_store.get("bob", alice["session_id"])
    assert error.value.status_code == 404
    with pytest.raises(HTTPException):
        await memory_store.runs("bob", alice["session_id"])
    with pytest.raises(HTTPException):
        await memory_store.remove("bob", alice["session_id"])


async def test_session_claim_serializes_prompts_and_preserves_activity(memory_store: SessionStore) -> None:
    session = await memory_store.create("alice", "conv_alice")
    record = RunRecord(
        id=str(uuid4()), session_id=session["session_id"], prompt_preview="Say hello", started_at=now(),
    )
    await memory_store.claim("alice", record)
    with pytest.raises(HTTPException) as error:
        await memory_store.claim("alice", record.model_copy(update={"id": str(uuid4())}))
    assert error.value.status_code == 409
    adapter = ResponseEvents(record, "https://example.services.ai.azure.com/api/projects/workplace")
    adapter.start_call({"call_id": "call_1", "id": "item_1", "name": "workiq___ask"}, observed=True)
    adapter.finish_call({"call_id": "call_1", "output": '{"isError":false}'})
    record.status = "completed"
    await memory_store.save_run("alice", record)
    await memory_store.release("alice", record)
    runs = await memory_store.runs("alice", session["session_id"])
    assert runs[0]["activity"][0]["name"] == "workiq___ask"
    assert runs[0]["activity"][0]["status"] == "completed"
    assert not (await memory_store.list("alice"))[0]["running"]
    await memory_store.remove("alice", session["session_id"])
    assert await memory_store.list("alice") == []


async def test_delete_marker_blocks_new_prompts_and_can_be_rolled_back(memory_store: SessionStore) -> None:
    session = await memory_store.create("alice", "conv_alice")
    record = RunRecord(
        id=str(uuid4()), session_id=session["session_id"], prompt_preview="Hello", started_at=now(),
    )
    marked = await memory_store.begin_delete("alice", record.session_id)
    with pytest.raises(HTTPException) as error:
        await memory_store.claim("alice", record)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        await memory_store.rename("alice", record.session_id, "Changed")
    await memory_store.cancel_delete(marked)
    await memory_store.claim("alice", record)
    with pytest.raises(HTTPException):
        await memory_store.begin_delete("alice", record.session_id)


async def test_stream_heartbeats_preserve_pending_event_and_close_upstream() -> None:
    closed = False

    async def source():
        nonlocal closed
        try:
            await asyncio.sleep(0.03)
            yield {"type": "response.created"}
            await asyncio.sleep(10)
        finally:
            closed = True

    stream = with_heartbeats(source(), interval=0.01)
    assert await anext(stream) is None
    result = None
    while result is None:
        result = await anext(stream)
    assert result["type"] == "response.created"
    await stream.aclose()
    assert closed


def test_stream_maps_actual_tool_skill_and_text_without_private_reasoning() -> None:
    record = RunRecord(
        id=str(uuid4()), session_id=str(uuid4()), prompt_preview="Find a source", started_at=now(),
    )
    adapter = ResponseEvents(record, "https://example.services.ai.azure.com/api/projects/workplace")
    assert adapter.consume({"type": "response.reasoning_text.delta", "delta": "private reasoning"}) == []
    emitted = adapter.consume({
        "type": "response.output_item.added",
        "item": {"id": "item_1", "call_id": "call_1", "name": "load_skill", "type": "function_call"},
    })
    assert emitted[0].type == EventType.TOOL_CALL_START
    arguments = '{"skill_name":"document-finder"}'
    adapter.consume({
        "type": "response.function_call_arguments.delta", "item_id": "item_1", "delta": arguments,
    })
    adapter.consume({
        "type": "response.function_call_arguments.done", "item_id": "item_1", "arguments": arguments,
    })
    adapter.consume({
        "type": "response.output_item.done",
        "item": {"type": "function_call_output", "call_id": "call_1", "output": '{"isError":false}'},
    })
    adapter.consume({"type": "response.output_text.delta", "item_id": "msg_1", "delta": "A public answer."})
    emitted = adapter.consume({
        "type": "response.completed",
        "response": {"id": "resp_1", "status": "completed", "output": [{
            "type": "message", "role": "assistant", "id": "msg_1",
            "content": [{"type": "output_text", "text": "A public answer."}],
        }]},
    })
    assert any(event.type == EventType.TEXT_MESSAGE_END for event in emitted)
    assert record.activity[0].kind == "skill"
    assert record.activity[0].skill == "document-finder"
    assert record.activity[0].status == "completed"
    assert record.assistant_message_ids == ["msg_1"]
    assert adapter.terminal


def test_tool_result_error_is_not_reported_as_success_or_replayed() -> None:
    record = RunRecord(id=str(uuid4()), session_id=str(uuid4()), prompt_preview="Hello", started_at=now())
    adapter = ResponseEvents(record, "https://example.services.ai.azure.com/api/projects/workplace")
    item = {"type": "function_call", "name": "workiq___ask", "call_id": "call_1", "arguments": "{}"}
    adapter.completed_item(item)
    result = {"type": "function_call_output", "call_id": "call_1", "output": '{"isError":true}'}
    adapter.completed_item(result)
    assert record.activity[0].status == "failed"
    assert adapter.completed_item(result) == []
    assert adapter.completed_item(item) == []


def test_file_metadata_never_upgrades_reported_locations_to_verified_pages() -> None:
    source = {"title": "Plan.pdf", "url": "https://contoso.sharepoint.com/plan", "page": 3}
    text = "A result.\n```workiq_references\n" + json.dumps([source]) + "\n```"
    references = references_from_text(text)
    assert references[0].page == 3
    assert references[0].location_verified is False
    source["location_verified"] = True
    assert references_from_text("```workiq_references\n" + json.dumps([source]) + "\n```") == []
    with pytest.raises(ValidationError):
        Reference(title="Plan", url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        Reference(title="Plan", url="https://contoso.sharepoint.com/plan", item_id="../other")


@pytest.mark.parametrize("url", [
    "https://evil.example.com/document",
    "https://sharepoint.com.evil.example.com/document",
    "https://contoso.sharepoint.com@evil.example.com/document",
    "http://contoso.sharepoint.com/document",
])
def test_preview_rejects_untrusted_hosts(url: str) -> None:
    with pytest.raises(ValueError):
        validate_preview_url(url)


async def test_preview_uses_delegated_graph_token_and_explains_position_fallback() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(400)
        return httpx.Response(200, json={"getUrl": "https://contoso.sharepoint.com/preview"})

    reference = Reference(
        title="Plan.pptx", url="https://contoso.sharepoint.com/plan",
        drive_id="b!drive", item_id="item1", slide=5,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        value = await preview_reference(client, "delegated-graph-token", reference)
    assert requests[0].headers["Authorization"] == "Bearer delegated-graph-token"
    assert json.loads(requests[0].content) == {"page": "5"}
    assert json.loads(requests[1].content) == {}
    assert value["warning"]
    assert value["position_verified"] is False


async def test_foundry_history_omits_private_reasoning_and_paginates(config: UIConfig) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"data": [
                {"id": "reasoning_1", "type": "reasoning", "summary": "private"},
                {"id": "user_1", "type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "Hello"}]},
            ], "has_more": True})
        return httpx.Response(200, json={"data": [
            {"id": "assistant_1", "type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Hello back"}]},
        ], "has_more": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        messages = await FoundryGateway(config, client).messages("delegated-user-token", "conv_1")
    assert [message["content"] for message in messages] == ["Hello", "Hello back"]
    assert requests[1].url.params["after"] == "user_1"
    assert all("/agents/workplace-productivity/endpoint/protocols/openai/conversations/" in str(
        request.url,
    ) for request in requests)
    assert all(request.url.params["api-version"] == "v1" for request in requests)
    assert all(request.headers["Authorization"] == "Bearer delegated-user-token" for request in requests)

async def test_all_new_conversation_operations_share_the_hosted_responses_scope(config: UIConfig) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "/agents/workplace-productivity/endpoint/protocols/openai/" in request.url.path
        assert request.url.params["api-version"] == "v1"
        if request.url.path.endswith("/responses"):
            assert json.loads(request.content)["conversation"] == "conv_hosted"
            return httpx.Response(200, text='data: {"type":"response.completed"}\n\n')
        if request.method == "POST":
            return httpx.Response(200, json={"id": "conv_hosted"})
        if request.method == "GET":
            return httpx.Response(200, json={"data": [], "has_more": False})
        return httpx.Response(200, json={"deleted": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway = FoundryGateway(config, client)
        identifier = await gateway.create_conversation("delegated")
        assert await gateway.messages("delegated", identifier) == []
        assert [event async for event in gateway.stream("delegated", identifier, "Hello")]
        await gateway.delete_conversation("delegated", identifier)
    assert len(requests) == 4


@pytest.fixture
def api_client(config: UIConfig, signing_key, memory_store: SessionStore):
    gateway = MagicMock()
    gateway.create_conversation = AsyncMock(return_value="conv_test")
    gateway.messages = AsyncMock(return_value=[])
    gateway.delete_conversation = AsyncMock()
    tokens = MagicMock(delegated=AsyncMock(return_value="delegated-user-token"))
    client = MagicMock()
    services = Services(authenticator(config, signing_key), tokens, memory_store, gateway, client)
    with TestClient(create_app(config, services)) as api:
        yield api, services


def test_api_requires_valid_user_and_checks_session_ownership_before_foundry(
    api_client, config: UIConfig, signing_key,
) -> None:
    client, services = api_client
    assert client.get("/api/sessions").status_code == 401
    alice = {"Authorization": f"Bearer {user_token(config, signing_key)}"}
    response = client.post("/api/sessions", headers=alice, json={})
    assert response.status_code == 201
    identifier = response.json()["id"]
    bob = {"Authorization": f"Bearer {user_token(config, signing_key, oid=BOB)}"}
    services.tokens.delegated.reset_mock()
    assert client.get(f"/api/sessions/{identifier}", headers=bob).status_code == 404
    services.tokens.delegated.assert_not_awaited()
    assert client.get("/api/sessions", headers=bob).json() == []
    response = client.post("/api/sessions", headers={**alice, "Origin": "https://evil.example.com"})
    assert response.status_code == 403


def test_agui_stream_persists_per_prompt_activity_and_rejects_injected_tools(
    api_client, config: UIConfig, signing_key,
) -> None:
    client, services = api_client
    headers = {"Authorization": f"Bearer {user_token(config, signing_key)}"}
    identifier = client.post("/api/sessions", headers=headers, json={}).json()["id"]

    async def stream(token: str, conversation_id: str, prompt: str):
        assert token == "delegated-user-token"
        assert conversation_id == "conv_test"
        assert prompt == "Say hello only"
        yield {"type": "response.created", "response": {"id": "resp_1"}}
        yield {"type": "response.output_item.added", "item": {
            "type": "function_call", "id": "fc_1", "call_id": "call_1",
            "name": "workiq___ask", "arguments": "",
        }}
        yield {"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "call_1",
            "name": "workiq___ask", "arguments": '{"question":"hello"}',
        }}
        yield {"type": "response.output_item.done", "item": {
            "type": "function_call_output", "call_id": "call_1", "output": '{"isError":false}',
        }}
        yield {"type": "response.completed", "response": {
            "id": "resp_1", "status": "completed", "output": [{
                "id": "msg_1", "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello"}],
            }],
        }}

    services.foundry.stream = stream
    body = {
        "threadId": identifier, "runId": str(uuid4()), "state": {}, "tools": [], "context": [],
        "forwardedProps": {}, "messages": [{"id": "user_1", "role": "user", "content": "Say hello only"}],
    }
    response = client.post("/api/chat", headers=headers, json=body)
    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert events[0]["type"] == "RUN_STARTED"
    assert events[-1]["type"] == "RUN_FINISHED"
    assert any(
        event["type"] == "TOOL_CALL_START" and event["toolCallName"] == "workiq___ask" for event in events
    )
    runs = client.get(f"/api/sessions/{identifier}", headers=headers).json()["runs"]
    assert runs[0]["activity"][0]["name"] == "workiq___ask"
    assert runs[0]["assistant_message_ids"] == ["msg_1"]
    body["context"] = [{"description": "injected context", "value": "do not authenticate"}]
    assert client.post("/api/chat", headers=headers, json=body).status_code == 422


def test_session_delete_rolls_back_marker_if_foundry_rejects_request(
    api_client, config: UIConfig, signing_key,
) -> None:
    client, services = api_client
    headers = {"Authorization": f"Bearer {user_token(config, signing_key)}"}
    identifier = client.post("/api/sessions", headers=headers, json={}).json()["id"]
    services.foundry.delete_conversation.side_effect = HTTPException(
        status_code=403, detail="Access denied.",
    )
    assert client.delete(f"/api/sessions/{identifier}", headers=headers).status_code == 403
    assert client.patch(
        f"/api/sessions/{identifier}", headers=headers, json={"title": "Still usable"},
    ).status_code == 200
    services.foundry.delete_conversation.side_effect = None
    assert client.delete(f"/api/sessions/{identifier}", headers=headers).status_code == 200
    assert client.get(f"/api/sessions/{identifier}", headers=headers).status_code == 404


def test_auth_bridge_is_uncached_and_can_return_popup_and_same_origin_iframe_responses(
    api_client, config: UIConfig,
) -> None:
    client, _ = api_client
    (config.static_directory / "auth.html").write_text("<html>Microsoft Authentication</html>")
    (config.static_directory / "index.html").write_text("<html>Chat</html>")
    bridge = client.get("/auth.html")
    assert bridge.status_code == 200
    assert bridge.headers["Cache-Control"] == "no-store"
    assert "Cross-Origin-Opener-Policy" not in bridge.headers
    assert "frame-ancestors 'self'" in bridge.headers["Content-Security-Policy"]
    main = client.get("/")
    assert main.headers["Cross-Origin-Opener-Policy"] == "same-origin-allow-popups"
    assert "frame-ancestors 'none'" in main.headers["Content-Security-Policy"]


async def test_first_prompt_upgrades_an_empty_legacy_session_without_losing_catalog_or_trace(
    api_client, config: UIConfig, signing_key,
) -> None:
    client, services = api_client
    owner = Principal(TENANT, ALICE, "").owner(config)
    document = await services.store.create(owner, "conv_legacy")
    document.pop("conversation_scope")
    document["title"] = "Existing session"
    document = await services.store.replace(document)
    services.foundry.has_legacy_history = AsyncMock(return_value=False)
    services.foundry.create_conversation.return_value = "conv_hosted"

    async def stream(token: str, conversation_id: str, prompt: str):
        assert conversation_id == "conv_hosted"
        yield {"type": "response.completed", "response": {
            "id": "resp_hosted", "status": "completed", "output": [],
        }}

    services.foundry.stream = stream
    headers = {"Authorization": f"Bearer {user_token(config, signing_key)}"}
    identifier = document["session_id"]
    body = {
        "threadId": identifier, "runId": str(uuid4()), "state": {}, "tools": [], "context": [],
        "forwardedProps": {},
        "messages": [{"id": "user_1", "role": "user", "content": "Hello"}],
    }
    response = client.post("/api/chat", headers=headers, json=body)
    assert response.status_code == 200
    assert "RUN_FINISHED" in response.text
    updated = await services.store.get(owner, identifier)
    assert updated["conversation_id"] == "conv_hosted"
    assert updated["legacy_conversation_id"] == "conv_legacy"
    assert updated["conversation_scope"] == "hosted"
    assert updated["title"] == "Existing session"
    assert len(await services.store.runs(owner, identifier)) == 1
    assert client.delete(f"/api/sessions/{identifier}", headers=headers).status_code == 200
    deleted = services.foundry.delete_conversation.await_args_list
    assert [(call.args[1], call.kwargs["legacy"]) for call in deleted] == [
        ("conv_legacy", True), ("conv_hosted", False),
    ]


async def test_legacy_session_with_history_is_never_silently_replaced(
    api_client, config: UIConfig, signing_key,
) -> None:
    client, services = api_client
    owner = Principal(TENANT, ALICE, "").owner(config)
    document = await services.store.create(owner, "conv_legacy")
    document.pop("conversation_scope")
    document = await services.store.replace(document)
    services.foundry.has_legacy_history = AsyncMock(return_value=True)
    headers = {"Authorization": f"Bearer {user_token(config, signing_key)}"}
    response = client.post("/api/chat", headers=headers, json={
        "threadId": document["session_id"], "runId": str(uuid4()), "state": {},
        "tools": [], "context": [], "forwardedProps": {},
        "messages": [{"id": "user_1", "role": "user", "content": "Hello"}],
    })
    assert response.status_code == 409
    assert "history is retained" in response.json()["detail"]
    services.foundry.create_conversation.assert_not_awaited()
    assert (await services.store.get(owner, document["session_id"]))["conversation_id"] == "conv_legacy"


@pytest.mark.parametrize("library_url", [
    "https://contoso.sharepoint.com/sites/Research/Shared%20Documents",
    "https://contoso.sharepoint.com/sites/Research/Shared%20Documents/Forms/AllItems.aspx",
])
async def test_preview_resolves_library_view_url_using_only_delegated_graph_requests(
    library_url: str,
) -> None:
    requests = []
    target = "/sites/Research/Shared Documents/Folder/Plan.pdf"
    source = Reference(
        title="Plan", page=2,
        url="https://contoso.sharepoint.com/sites/Research/Shared%20Documents/Forms/AllItems.aspx?"
        + urlencode({"id": target}),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "graph.microsoft.com"
        assert request.headers["Authorization"] == "Bearer delegated-graph-token"
        path = request.url.path
        if path.endswith("/sites/contoso.sharepoint.com:/sites/Research"):
            return httpx.Response(200, json={"id": "site1"})
        if path.endswith("/sites/site1/drives"):
            return httpx.Response(200, json={"value": [{
                "id": "b!drive1", "webUrl": library_url,
            }]})
        if path.endswith("/drives/b!drive1/root:/Folder/Plan.pdf"):
            return httpx.Response(200, json={"id": "item1", "file": {"mimeType": "application/pdf"}})
        assert path.endswith("/drives/b!drive1/items/item1/preview")
        assert json.loads(request.content) == {"page": "2"}
        return httpx.Response(200, json={"getUrl": "https://contoso.sharepoint.com/preview"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await preview_reference(client, "delegated-graph-token", source)
    assert len(requests) == 4
    assert result["position_verified"] is False
    assert result["get_url"] == "https://contoso.sharepoint.com/preview"


@pytest.mark.parametrize("site_path", ["", "/sites/Research", "/personal/alice_contoso_com"])
async def test_preview_resolves_viewer_guid_to_actual_graph_ids_without_guessing_a_file_path(
    site_path: str,
) -> None:
    unique_id = "11111111-2222-4333-8444-555555555555"
    source = Reference(
        title="Slides", slide=3,
        url=f"https://contoso.sharepoint.com{site_path}/_layouts/15/Doc.aspx?"
        + urlencode({
            "sourceDoc": "{" + unique_id.upper() + "}", "file": "Misleading-name.pptx", "action": "edit",
        }),
    )
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "graph.microsoft.com"
        assert request.headers["Authorization"] == "Bearer delegated-graph-token"
        site_address = f"contoso.sharepoint.com:{site_path}" if site_path else "contoso.sharepoint.com"
        if request.url.path == f"/v1.0/sites/{site_address}":
            return httpx.Response(200, json={"id": "site1"})
        if request.url.path == "/v1.0/sites/site1/drives":
            return httpx.Response(200, json={"value": [
                {"id": "b!other", "webUrl": f"https://contoso.sharepoint.com{site_path}/Other"},
                {"id": "b!actual", "webUrl": f"https://contoso.sharepoint.com{site_path}/Documents"},
            ]})
        if request.url.path == f"/v1.0/drives/b!other/items/{unique_id}":
            return httpx.Response(404)
        if request.url.path == f"/v1.0/drives/b!actual/items/{unique_id}":
            assert request.url.params["$select"] == "id,file"
            return httpx.Response(200, json={"id": "opaque-item1", "file": {}})
        assert request.url.path == "/v1.0/drives/b!actual/items/opaque-item1/preview"
        assert request.method == "POST"
        assert json.loads(request.content) == {"page": "3"}
        return httpx.Response(200, json={"getUrl": "https://contoso.sharepoint.com/preview"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await preview_reference(client, "delegated-graph-token", source)
    assert len(requests) == 5
    assert result["position_verified"] is False
    assert result["get_url"] == "https://contoso.sharepoint.com/preview"
    assert all("Misleading-name" not in str(request.url) for request in requests)


@pytest.mark.parametrize("status", [403, 404])
async def test_viewer_guid_resolution_reports_access_denial_or_missing_file_without_preview(
    status: int,
) -> None:
    source = Reference(
        title="Slides",
        url="https://contoso.sharepoint.com/sites/Research/_layouts/15/Doc.aspx?"
        "sourcedoc=11111111-2222-4333-8444-555555555555",
    )
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        if len(requests) == 1:
            return httpx.Response(200, json={"id": "site1"})
        if len(requests) == 2:
            return httpx.Response(200, json={"value": [{
                "id": "b!drive", "webUrl": "https://contoso.sharepoint.com/sites/Research/Docs",
            }]})
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as error:
            await preview_reference(client, "delegated-graph-token", source)
    assert error.value.status_code == status
    assert len(requests) == 3


@pytest.mark.parametrize("url", [
    "https://evil.example.com/Plan.pdf",
    "https://contoso.sharepoint.com/_layouts/15/Doc.aspx?sourcedoc=unknown",
    "https://contoso.sharepoint.com/_layouts/15/Doc.aspx?file=Slides.pptx",
    "https://contoso.sharepoint.com/_layouts/15/Doc.aspx?sourcedoc=",
    "https://contoso.sharepoint.com/_layouts/15/Doc.aspx?"
    "sourcedoc=11111111-2222-4333-8444-555555555555&SourceDoc=11111111-2222-4333-8444-555555555555",
    "https://contoso.sharepoint.com/sites/../_layouts/15/Doc.aspx?"
    "sourcedoc=11111111-2222-4333-8444-555555555555",
    "https://contoso.sharepoint.com/sites/Research/Shared%20Documents/Forms/AllItems.aspx",
    "https://contoso.sharepoint.com/sites/Research/Shared%20Documents/Forms/AllItems.aspx?id=%2Fother%2FPlan.pdf",
    "https://contoso.sharepoint.com/sites/Research/Shared%20Documents/../private.pdf",
])
def test_sharepoint_file_resolution_rejects_unsupported_or_ambiguous_targets(url: str) -> None:
    with pytest.raises(ValueError):
        sharepoint_file(url)


async def test_preview_does_not_forward_credentials_to_an_untrusted_pagination_link() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"id": "site1"})
        return httpx.Response(200, json={
            "value": [], "@odata.nextLink": "https://evil.example.com/v1.0/sites/site1/drives",
        })

    source = Reference(title="Plan", url="https://contoso.sharepoint.com/sites/Research/Docs/Plan.pdf")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as error:
            await preview_reference(client, "delegated-graph-token", source)
    assert error.value.status_code == 502
    assert len(requests) == 2
    assert all(request.url.host == "graph.microsoft.com" for request in requests)


async def test_preview_keeps_graph_access_denials_explicit() -> None:
    source = Reference(title="Plan", url="https://contoso.sharepoint.com/sites/Research/Docs/Plan.pdf")
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(HTTPException) as error:
            await preview_reference(client, "delegated-graph-token", source)
    assert error.value.status_code == 403


def test_tool_payloads_are_bounded_and_redact_credentials_and_temporary_authentication_urls() -> None:
    text, truncated = payload_text(json.dumps({
        "question": "Hello", "nested": {"access_token": "private-token", "clientSecret": "private-secret"},
        "header": "Bearer private-bearer",
        "url": "https://contoso.sharepoint.com/file?sig=private-signature",
    }))
    assert "Hello" in text
    assert all(
        value not in text
        for value in ("private-token", "private-secret", "private-bearer", "private-signature")
    )
    assert "[redacted" in text
    assert not truncated
    large, truncated = payload_text("x" * 70_000)
    assert truncated
    assert len(large) < 70_000


async def test_tool_payload_lookup_returns_only_the_requested_actual_call_not_private_reasoning(
    config: UIConfig,
) -> None:
    record = RunRecord(
        id=str(uuid4()), session_id=str(uuid4()), response_id="resp1", status="completed",
        prompt_preview="Hello", started_at=now(),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/endpoint/protocols/openai/responses/resp1")
        assert request.url.params["api-version"] == "v1"
        return httpx.Response(200, json={"output": [
            {"type": "reasoning", "content": "private reasoning must not be returned"},
            {"type": "function_call", "call_id": "other", "arguments": '{"secret":"other call"}'},
            {"type": "function_call", "call_id": "call1", "arguments": '{"question":"hello"}'},
            {"type": "function_call_output", "call_id": "call1", "output": '{"answer":"hello"}'},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await FoundryGateway(config, client).tool_details("delegated", record, "call1")
    assert json.loads(result["input"]) == {"question": "hello"}
    assert json.loads(result["output"]) == {"answer": "hello"}
    assert "private reasoning" not in json.dumps(result)
    assert "other call" not in json.dumps(result)


async def test_tool_payload_api_checks_user_session_run_and_call_before_any_upstream_access(
    api_client, config: UIConfig, signing_key,
) -> None:
    client, services = api_client
    owner = Principal(TENANT, ALICE, "").owner(config)
    session = await services.store.create(owner, "conv1")
    record = RunRecord(
        id=str(uuid4()), session_id=session["session_id"], response_id="resp1", status="completed",
        prompt_preview="Hello", started_at=now(),
        activity=[ToolActivity(call_id="call1", name="workiq___ask", started_at=now(), status="completed")],
    )
    await services.store.save_run(owner, record)
    services.foundry.tool_details = AsyncMock(return_value={
        "input": '{"question":"hello"}', "output": '{"answer":"hello"}',
        "input_truncated": False, "output_truncated": False,
    })
    bob = {"Authorization": f"Bearer {user_token(config, signing_key, oid=BOB)}"}
    route = f"/api/sessions/{record.session_id}/runs/{record.id}/calls/"
    assert client.get(route + "call1", headers=bob).status_code == 404
    alice = {"Authorization": f"Bearer {user_token(config, signing_key)}"}
    assert client.get(route + "unknown", headers=alice).status_code == 404
    services.tokens.delegated.assert_not_awaited()
    response = client.get(route + "call1", headers=alice)
    assert response.status_code == 200
    assert json.loads(response.json()["output"]) == {"answer": "hello"}
    stored = (await services.store.runs(owner, record.session_id))[0]["activity"][0]
    assert "input" not in stored and "output" not in stored
