from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

from azure.core import MatchConditions
from azure.cosmos.aio import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from fastapi import HTTPException

from .models import RunRecord


def now() -> str:
    return datetime.now(UTC).isoformat()


def session_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid session ID.") from error


def public_session(document: dict) -> dict:
    return {
        "id": document["session_id"],
        "title": document["title"],
        "created_at": document["created_at"],
        "updated_at": document["updated_at"],
        "running": document.get("lease_until", 0) > time.time(),
    }


class SessionStore:
    def __init__(self, container: ContainerProxy) -> None:
        self.container = container

    async def ready(self) -> None:
        await self.container.read()

    async def create(self, owner: str, conversation_id: str) -> dict:
        identifier = str(uuid4())
        timestamp = now()
        document = {
            "id": f"session:{identifier}", "owner": owner, "kind": "session",
            "session_id": identifier, "conversation_id": conversation_id,
            "conversation_scope": "hosted",
            "title": "New conversation", "created_at": timestamp, "updated_at": timestamp,
            "lease_until": 0,
        }
        return await self.container.create_item(document)

    async def get(self, owner: str, identifier: str) -> dict:
        try:
            return await self.container.read_item(
                f"session:{session_id(identifier)}", partition_key=owner,
            )
        except CosmosResourceNotFoundError as error:
            raise HTTPException(status_code=404, detail="Session not found.") from error

    async def list(self, owner: str) -> list[dict]:
        return [
            public_session(document)
            async for document in self.container.query_items(
                "SELECT * FROM c WHERE c.kind = 'session' ORDER BY c.updated_at DESC",
                partition_key=owner,
            )
        ]

    async def replace(self, document: dict) -> dict:
        body = {key: value for key, value in document.items() if not key.startswith("_")}
        try:
            return await self.container.replace_item(
                document["id"], body, etag=document["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as error:
            if error.status_code != 412:
                raise
            raise HTTPException(status_code=409, detail="Session changed; reload it and retry.") from error

    async def rename(self, owner: str, identifier: str, title: str) -> dict:
        document = await self.get(owner, identifier)
        self.check_not_deleting(document)
        document.update(title=title, updated_at=now())
        return await self.replace(document)

    async def attach_hosted_conversation(self, document: dict, conversation_id: str) -> dict:
        self.check_not_deleting(document)
        if public_session(document)["running"]:
            raise HTTPException(status_code=409, detail="Wait for this session's running prompt to finish.")
        document.update(
            legacy_conversation_id=document["conversation_id"],
            conversation_id=conversation_id, conversation_scope="hosted", updated_at=now(),
        )
        return await self.replace(document)

    @staticmethod
    def check_not_deleting(document: dict) -> None:
        if document.get("deleting"):
            raise HTTPException(status_code=409, detail="This session is being deleted.")

    async def claim(self, owner: str, record: RunRecord) -> dict:
        document = await self.get(owner, record.session_id)
        self.check_not_deleting(document)
        if document.get("lease_until", 0) > time.time():
            raise HTTPException(status_code=409, detail="This session already has a running prompt.")
        document.update(run_id=record.id, lease_until=time.time() + 600, updated_at=now())
        if document["title"] == "New conversation":
            document["title"] = record.prompt_preview[:60]
        document = await self.replace(document)
        try:
            await self.container.create_item(self.run_document(owner, record))
        except CosmosResourceExistsError as error:
            await self.release(owner, record)
            raise HTTPException(status_code=409, detail="Run IDs cannot be reused.") from error
        return document

    @staticmethod
    def run_document(owner: str, record: RunRecord) -> dict:
        return {
            **record.model_dump(mode="json"),
            "id": f"run:{record.id}", "run_id": record.id, "owner": owner, "kind": "run",
        }

    async def save_run(self, owner: str, record: RunRecord) -> None:
        await self.container.upsert_item(self.run_document(owner, record))

    async def release(self, owner: str, record: RunRecord) -> None:
        for attempt in range(3):
            document = await self.get(owner, record.session_id)
            if document.get("run_id") != record.id:
                return
            document.update(lease_until=0, updated_at=now())
            try:
                await self.replace(document)
                return
            except HTTPException as error:
                if error.status_code != 409 or attempt == 2:
                    raise

    async def runs(self, owner: str, identifier: str) -> list[dict]:
        await self.get(owner, identifier)
        documents = self.container.query_items(
            "SELECT * FROM c WHERE c.kind = 'run' AND c.session_id = @session ORDER BY c.started_at ASC",
            parameters=[{"name": "@session", "value": session_id(identifier)}],
            partition_key=owner,
        )
        return [
            RunRecord.model_validate({**document, "id": document["run_id"]}).model_dump(mode="json")
            async for document in documents
        ]

    async def get_run(self, owner: str, identifier: str, run_id: str) -> RunRecord:
        await self.get(owner, identifier)
        try:
            run_id = str(UUID(run_id))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid run ID.") from error
        try:
            document = await self.container.read_item(f"run:{run_id}", partition_key=owner)
        except CosmosResourceNotFoundError as error:
            raise HTTPException(status_code=404, detail="Run not found.") from error
        if document["session_id"] != session_id(identifier):
            raise HTTPException(status_code=404, detail="Run not found.")
        return RunRecord.model_validate({**document, "id": document["run_id"]})

    async def begin_delete(self, owner: str, identifier: str) -> dict:
        document = await self.get(owner, identifier)
        if document.get("deleting"):
            return document
        if document.get("lease_until", 0) > time.time():
            raise HTTPException(
                status_code=409, detail="Stop the running prompt before deleting this session.",
            )
        document.update(deleting=True, updated_at=now())
        return await self.replace(document)

    async def cancel_delete(self, document: dict) -> None:
        document.update(deleting=False, updated_at=now())
        await self.replace(document)

    async def remove(self, owner: str, identifier: str) -> None:
        document = await self.begin_delete(owner, identifier)
        records = [
            record async for record in self.container.query_items(
                "SELECT c.id FROM c WHERE c.kind = 'run' AND c.session_id = @session",
                parameters=[{"name": "@session", "value": session_id(identifier)}],
                partition_key=owner,
            )
        ]
        for record in records:
            try:
                await self.container.delete_item(record["id"], partition_key=owner)
            except CosmosResourceNotFoundError:
                continue
        await self.container.delete_item(document["id"], partition_key=owner)
