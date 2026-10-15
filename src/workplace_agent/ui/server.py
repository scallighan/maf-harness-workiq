from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import httpx
import uvicorn
from ag_ui.core import CustomEvent, RunAgentInput, RunErrorEvent, RunFinishedEvent, RunStartedEvent
from ag_ui.encoder import EventEncoder
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.identity.aio import ManagedIdentityCredential
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auth import Authenticator, Principal, TokenBroker
from .config import UIConfig
from .events import ResponseEvents
from .foundry import FoundryGateway, preview_reference
from .models import PreviewRequest, RenameSession, RunRecord
from .store import SessionStore, now, public_session, session_id

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)

async def with_heartbeats(source: AsyncGenerator[dict], interval: float = 15) -> AsyncGenerator[dict | None]:
    async with aclosing(source):
        pending = asyncio.create_task(anext(source))
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=interval)
                if not done:
                    yield None
                    continue
                try:
                    yield pending.result()
                except StopAsyncIteration:
                    return
                pending = asyncio.create_task(anext(source))
        finally:
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending


@dataclass
class Services:
    auth: Authenticator
    tokens: TokenBroker
    store: SessionStore
    foundry: FoundryGateway
    client: httpx.AsyncClient


async def hosted_conversation(current: Services, document: dict, token: str) -> dict:
    if document.get("conversation_scope") == "hosted":
        return document
    if await current.foundry.has_legacy_history(token, document["conversation_id"]):
        raise HTTPException(
            status_code=409,
            detail=(
                "This session has history in the old conversation store. "
                "Start a new session; history is retained."
            ),
        )
    conversation_id = await current.foundry.create_conversation(token)
    try:
        return await current.store.attach_hosted_conversation(document, conversation_id)
    except (HTTPException, CosmosHttpResponseError, ClientAuthenticationError, ServiceRequestError):
        await current.foundry.delete_conversation(token, conversation_id)
        raise


async def signed_in_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> Principal:
    if credentials is None:
        raise HTTPException(
            status_code=401, detail="Sign in to use this API.", headers={"WWW-Authenticate": "Bearer"},
        )
    services: Services = request.app.state.services
    return await asyncio.to_thread(services.auth.validate, credentials.credentials)


SignedInUser = Annotated[Principal, Depends(signed_in_user)]


def create_app(config: UIConfig | None = None, services: Services | None = None) -> FastAPI:
    settings = config or UIConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if services is not None:
            app.state.services = services
            yield
            return
        async with AsyncExitStack() as resources:
            credential = ManagedIdentityCredential(client_id=settings.identity_client_id)
            resources.push_async_callback(credential.close)
            cosmos = CosmosClient(settings.cosmos_endpoint, credential=credential)
            resources.push_async_callback(cosmos.close)
            container = cosmos.get_database_client(settings.cosmos_database).get_container_client(
                settings.cosmos_container,
            )
            client = await resources.enter_async_context(httpx.AsyncClient(
                timeout=httpx.Timeout(180, connect=20), follow_redirects=False,
            ))
            tokens = TokenBroker(settings)
            resources.callback(tokens.close)
            app.state.services = Services(
                Authenticator(settings), tokens, SessionStore(container),
                FoundryGateway(settings, client), client,
            )
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        origin = request.headers.get("origin")
        if (
            request.method not in ("GET", "HEAD") and origin
            and origin not in (settings.origin, *settings.additional_origins)
        ):
            return JSONResponse(status_code=403, content={"detail": "Unexpected request origin."})
        response = await call_next(request)
        auth_bridge = request.url.path == "/auth.html"
        frame_ancestors = "'self'" if auth_bridge else "'none'"
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "connect-src 'self' https://login.microsoftonline.com; "
                "img-src 'self' data:; object-src 'none'; base-uri 'self'; "
                f"frame-ancestors {frame_ancestors}; "
                "frame-src 'self' https://login.microsoftonline.com "
                "https://*.sharepoint.com https://*.sharepoint-df.com "
                "https://*.officeapps.live.com https://*.office.com https://*.onedrive.com "
                "https://onedrive.live.com https://*.microsoft365.com; "
                "form-action 'self' https://*.sharepoint.com https://*.officeapps.live.com "
                "https://*.office.com https://*.onedrive.com https://onedrive.live.com"
            ),
        })
        if not auth_bridge:
            response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        return response

    @app.get("/healthz")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready(request: Request) -> dict:
        try:
            await request.app.state.services.store.ready()
        except (
            CosmosHttpResponseError, httpx.HTTPError, ClientAuthenticationError, ServiceRequestError,
        ) as error:
            logger.warning("UI session store readiness failed: %s", type(error).__name__)
            raise HTTPException(status_code=503, detail="Session storage is not ready.") from error
        return {"status": "ready"}

    @app.get("/api/config")
    async def public_config() -> dict:
        return settings.public()

    @app.get("/api/sessions")
    async def list_sessions(request: Request, principal: SignedInUser) -> list[dict]:
        return await request.app.state.services.store.list(principal.owner(settings))

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: Request, principal: SignedInUser) -> dict:
        current: Services = request.app.state.services
        token = await current.tokens.delegated(principal, "https://ai.azure.com")
        conversation_id = await current.foundry.create_conversation(token)
        try:
            document = await current.store.create(principal.owner(settings), conversation_id)
        except (CosmosHttpResponseError, ServiceRequestError, ClientAuthenticationError):
            await current.foundry.delete_conversation(token, conversation_id)
            raise
        return public_session(document)

    @app.get("/api/sessions/{identifier}")
    async def get_session(
        identifier: str, request: Request, principal: SignedInUser,
    ) -> dict:
        current: Services = request.app.state.services
        owner = principal.owner(settings)
        document = await current.store.get(owner, identifier)
        token = await current.tokens.delegated(principal, "https://ai.azure.com")
        messages = await current.foundry.messages(
            token, document["conversation_id"], legacy=document.get("conversation_scope") != "hosted",
        )
        return {
            "session": public_session(document), "messages": messages,
            "runs": await current.store.runs(owner, identifier),
        }

    @app.patch("/api/sessions/{identifier}")
    async def rename_session(
        identifier: str, body: RenameSession, request: Request,
        principal: SignedInUser,
    ) -> dict:
        document = await request.app.state.services.store.rename(
            principal.owner(settings), identifier, body.title,
        )
        return public_session(document)

    @app.get("/api/sessions/{identifier}/runs/{run_id}/calls/{call_id}")
    async def tool_details(
        identifier: str, run_id: str, call_id: str, request: Request, principal: SignedInUser,
    ) -> dict:
        current: Services = request.app.state.services
        record = await current.store.get_run(principal.owner(settings), identifier, run_id)
        if not any(call.call_id == call_id for call in record.activity):
            raise HTTPException(status_code=404, detail="Tool call not found.")
        token = await current.tokens.delegated(principal, "https://ai.azure.com")
        return await current.foundry.tool_details(token, record, call_id)

    @app.delete("/api/sessions/{identifier}")
    async def delete_session(
        identifier: str, request: Request, principal: SignedInUser,
    ) -> dict:
        current: Services = request.app.state.services
        owner = principal.owner(settings)
        await current.store.get(owner, identifier)
        token = await current.tokens.delegated(principal, "https://ai.azure.com")
        document = await current.store.begin_delete(owner, identifier)
        try:
            if document.get("legacy_conversation_id"):
                await current.foundry.delete_conversation(
                    token, document["legacy_conversation_id"], legacy=True,
                )
            await current.foundry.delete_conversation(
                token, document["conversation_id"], legacy=document.get("conversation_scope") != "hosted",
            )
        except (HTTPException, httpx.HTTPError):
            await current.store.cancel_delete(document)
            raise
        await current.store.remove(owner, identifier)
        return {"deleted": True}

    @app.post("/api/sessions/{identifier}/preview")
    async def preview(
        identifier: str, body: PreviewRequest, request: Request,
        principal: SignedInUser,
    ) -> dict:
        current: Services = request.app.state.services
        await current.store.get(principal.owner(settings), identifier)
        token = await current.tokens.delegated(principal, "https://graph.microsoft.com")
        return await preview_reference(current.client, token, body.reference)

    @app.post("/api/chat")
    async def chat(
        body: RunAgentInput, request: Request, principal: SignedInUser,
    ) -> StreamingResponse:
        current: Services = request.app.state.services
        identifier = session_id(body.thread_id)
        try:
            run_id = str(UUID(body.run_id))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid run ID.") from error
        if body.tools or body.context or body.resume:
            raise HTTPException(
                status_code=422, detail="Client-supplied tools, context and approvals are unsupported.",
            )
        if not body.messages or body.messages[-1].role != "user":
            raise HTTPException(status_code=422, detail="A final user message is required.")
        prompt = body.messages[-1].content
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20_000:
            raise HTTPException(
                status_code=422, detail="Send a nonempty text prompt of at most 20,000 characters.",
            )
        owner = principal.owner(settings)
        document = await current.store.get(owner, identifier)
        token = await current.tokens.delegated(principal, "https://ai.azure.com")
        document = await hosted_conversation(current, document, token)
        record = RunRecord(
            id=run_id, session_id=identifier, prompt_preview=prompt.strip()[:160], started_at=now(),
        )
        document = await current.store.claim(owner, record)
        encoder = EventEncoder(accept="text/event-stream")

        async def events() -> AsyncIterator[str]:
            adapter = ResponseEvents(record, settings.project_endpoint)
            released = False
            try:
                yield encoder.encode(RunStartedEvent(thread_id=identifier, run_id=run_id))
                yield encoder.encode(adapter.activity_event())
                async with asyncio.timeout(540):
                    async for raw in with_heartbeats(
                        current.foundry.stream(token, document["conversation_id"], prompt),
                    ):
                        if raw is None:
                            yield ": keep-alive\n\n"
                            continue
                        for event in adapter.consume(raw):
                            if isinstance(event, CustomEvent) and event.name == "workiq.activity":
                                await current.store.save_run(owner, record)
                            yield encoder.encode(event)
                if not adapter.terminal:
                    raise HTTPException(
                        status_code=502, detail="The hosted agent stream ended without a result.",
                    )
                await current.store.save_run(owner, record)
                await current.store.release(owner, record)
                released = True
                yield encoder.encode(adapter.activity_event())
                yield encoder.encode(RunFinishedEvent(
                    thread_id=identifier, run_id=run_id, result={"status": record.status},
                ))
            except asyncio.CancelledError:
                adapter.interrupt(failed=False)
                raise
            except (
                HTTPException, httpx.HTTPError, json.JSONDecodeError, CosmosHttpResponseError, TimeoutError,
                ClientAuthenticationError, ServiceRequestError,
            ) as error:
                adapter.interrupt(failed=True)
                logger.warning("Hosted UI run failed: %s", type(error).__name__)
                detail = (
                    error.detail if isinstance(error, HTTPException)
                    else "The request failed; reload and retry."
                )
                yield encoder.encode(adapter.activity_event())
                yield encoder.encode(RunErrorEvent(message=str(detail), code="hosted_request_failed"))
            finally:
                if not released:
                    await asyncio.shield(current.store.save_run(owner, record))
                    await asyncio.shield(current.store.release(owner, record))

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )

    @app.get("/{path:path}")
    async def frontend(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found.")
        root = settings.static_directory.resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            raise HTTPException(status_code=404, detail="File not found.")
        if candidate.is_file():
            return FileResponse(candidate)
        index = root / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=503, detail="The UI assets have not been built.")
        return FileResponse(index)

    return app


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    uvicorn.run(create_app(), host="0.0.0.0", port=8080, access_log=False)


if __name__ == "__main__":
    main()
