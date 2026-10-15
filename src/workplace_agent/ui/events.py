from __future__ import annotations

import json
from urllib.parse import urlsplit
from uuid import uuid4

from ag_ui.core import (
    BaseEvent,
    CustomEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from fastapi import HTTPException

from ..execution import tool_output_failed, unwrap_tool_output
from .models import RunRecord, ToolActivity
from .references import references_from_text
from .store import now

SKILL_TOOLS = {"load_skill", "read_skill_resource", "run_skill_script"}


class ResponseEvents:
    def __init__(self, record: RunRecord, project_endpoint: str) -> None:
        self.record = record
        self.project_host = urlsplit(project_endpoint).hostname
        self.text: dict[str, str] = {}
        self.ended_messages: set[str] = set()
        self.calls: dict[str, ToolActivity] = {}
        self.arguments: dict[str, str] = {}
        self.ended_arguments: set[str] = set()
        self.returned_calls: set[str] = set()
        self.item_calls: dict[str, str] = {}
        self.terminal = False

    def activity_event(self) -> CustomEvent:
        return CustomEvent(name="workiq.activity", value=self.record.model_dump(mode="json"))

    def start_text(self, identifier: str) -> list[BaseEvent]:
        if identifier in self.text:
            return []
        self.text[identifier] = ""
        self.record.assistant_message_ids.append(identifier)
        return [TextMessageStartEvent(message_id=identifier, role="assistant")]

    def finish_text(self, identifier: str) -> list[BaseEvent]:
        if identifier in self.ended_messages:
            return []
        self.ended_messages.add(identifier)
        return [TextMessageEndEvent(message_id=identifier)]

    def start_call(self, item: dict) -> list[BaseEvent]:
        identifier, name = item.get("call_id"), item.get("name")
        if not isinstance(identifier, str) or not isinstance(name, str):
            raise HTTPException(status_code=502, detail="Foundry returned an invalid tool call.")
        if identifier in self.calls:
            return []
        activity = ToolActivity(
            call_id=identifier, name=name, started_at=now(),
            kind="skill" if name in SKILL_TOOLS else "tool",
        )
        self.calls[identifier] = activity
        if isinstance(item.get("id"), str):
            self.item_calls[item["id"]] = identifier
        self.arguments[identifier] = ""
        self.record.activity.append(activity)
        return [
            ToolCallStartEvent(tool_call_id=identifier, tool_call_name=name),
            self.activity_event(),
        ]

    def finish_arguments(self, identifier: str, arguments: str) -> list[BaseEvent]:
        if identifier in self.ended_arguments:
            return []
        events: list[BaseEvent] = []
        if not self.arguments.get(identifier) and arguments:
            events.append(ToolCallArgsEvent(tool_call_id=identifier, delta=arguments))
            self.arguments[identifier] = arguments
        activity = self.calls[identifier]
        if activity.kind == "skill":
            try:
                values = json.loads(arguments)
            except json.JSONDecodeError as error:
                raise HTTPException(
                    status_code=502, detail="Foundry returned invalid skill arguments.",
                ) from error
            if isinstance(values, dict):
                name = values.get("skill_name") or values.get("skill") or values.get("name")
                if isinstance(name, str) and name:
                    activity.skill = name[:256]
        self.ended_arguments.add(identifier)
        events.extend([ToolCallEndEvent(tool_call_id=identifier), self.activity_event()])
        return events

    def finish_call(self, item: dict) -> list[BaseEvent]:
        identifier = item.get("call_id")
        if identifier not in self.calls:
            raise HTTPException(status_code=502, detail="Foundry returned a result for an unknown tool call.")
        if identifier in self.returned_calls:
            return []
        self.returned_calls.add(identifier)
        activity = self.calls[identifier]
        try:
            output, timing = unwrap_tool_output(
                item.get("output"), call_id=identifier, name=activity.name,
            )
        except ValueError as error:
            raise HTTPException(status_code=502, detail="Foundry returned invalid tool timing.") from error
        failed = tool_output_failed(output, skill=activity.kind == "skill") or bool(timing and timing.failed)
        activity.status = "failed" if failed else "completed"
        if timing:
            activity.started_at = timing.started_at.isoformat()
            activity.duration_ms = timing.duration_ms
            activity.timing_source = "runtime"
        return [
            ToolCallResultEvent(
                message_id=f"tool-{uuid4()}", tool_call_id=identifier,
                content=json.dumps({"status": activity.status}),
            ),
            self.activity_event(),
        ]

    def consent_event(self, item: dict) -> CustomEvent:
        candidates = [item.get(key) for key in ("consent_link", "consent_url", "url", "authorization_url")]
        for value in candidates:
            if not isinstance(value, str):
                continue
            parsed = urlsplit(value)
            hostname = parsed.hostname or ""
            if (
                parsed.scheme == "https" and not parsed.username and not parsed.password
                and not any(ord(character) < 32 for character in value)
                and (
                    hostname in {"login.microsoftonline.com", "consent.azure-apim.net", self.project_host}
                    or hostname.endswith(".consent.azure-apim.net")
                )
            ):
                return CustomEvent(
                    name="workiq.consent",
                    value={"url": value, "response_id": self.record.response_id},
                )
        raise HTTPException(
            status_code=502, detail="Foundry requested consent without a supported HTTPS link.",
        )

    def completed_item(self, item: dict) -> list[BaseEvent]:
        kind = item.get("type")
        events: list[BaseEvent] = []
        if kind == "message" and item.get("role") == "assistant":
            identifier = item["id"]
            content = item.get("content", [])
            text = "".join(
                block.get("refusal", "") if block.get("type") == "refusal" else block.get("text", "")
                for block in content if block.get("type") in ("output_text", "refusal")
            )
            if identifier not in self.text and text:
                events.extend(self.start_text(identifier))
                self.text[identifier] = text
                events.append(TextMessageContentEvent(message_id=identifier, delta=text))
            if identifier in self.text:
                events.extend(self.finish_text(identifier))
                annotations = [
                    annotation for block in content for annotation in block.get("annotations", [])
                ]
                references = references_from_text(text or self.text[identifier], annotations)
                if references:
                    events.append(CustomEvent(
                        name="workiq.references",
                        value={
                            "message_id": identifier,
                            "references": [reference.model_dump(mode="json") for reference in references],
                        },
                    ))
        elif kind == "function_call":
            events.extend(self.start_call(item))
            events.extend(self.finish_arguments(item["call_id"], item.get("arguments", "{}")))
        elif kind == "function_call_output":
            events.extend(self.finish_call(item))
        elif kind == "oauth_consent_request":
            events.append(self.consent_event(item))
        elif kind in ("mcp_approval_request", "function_approval_request"):
            raise HTTPException(
                status_code=409, detail="The hosted agent requires approval; review its policy.",
            )
        return events

    def consume(self, event: dict) -> list[BaseEvent]:
        kind = event["type"]
        if kind == "response.created":
            self.record.response_id = event["response"]["id"]
            return [self.activity_event()]
        if kind in ("response.output_text.delta", "response.refusal.delta"):
            identifier, delta = event["item_id"], event["delta"]
            events = self.start_text(identifier)
            self.text[identifier] += delta
            events.append(TextMessageContentEvent(message_id=identifier, delta=delta))
            return events
        if kind == "response.output_item.added" and event["item"].get("type") == "function_call":
            return self.start_call(event["item"])
        if kind in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
            identifier = event.get("call_id")
            item_id = event.get("item_id")
            if not isinstance(identifier, str):
                identifier = self.item_calls.get(item_id) if isinstance(item_id, str) else None
            if not isinstance(identifier, str):
                raise HTTPException(status_code=502, detail="Foundry returned arguments for an unknown tool.")
            if kind == "response.function_call_arguments.delta":
                self.arguments[identifier] += event["delta"]
                return [ToolCallArgsEvent(tool_call_id=identifier, delta=event["delta"])]
            return self.finish_arguments(identifier, event["arguments"])
        if kind == "response.output_item.done":
            return self.completed_item(event["item"])
        if kind in ("response.completed", "response.incomplete", "response.failed"):
            response = event["response"]
            self.record.response_id = response["id"]
            events = [
                mapped for item in response.get("output", []) for mapped in self.completed_item(item)
            ]
            if response.get("error"):
                raise HTTPException(
                    status_code=502,
                    detail=f"Hosted agent failed ({response['error'].get('code', 'server_error')}).",
                )
            status = response.get("status")
            if status not in ("completed", "incomplete"):
                raise HTTPException(status_code=502, detail="Hosted agent did not complete its response.")
            self.record.status = status
            self.terminal = True
            events.append(self.activity_event())
            return events
        if kind == "error":
            raise HTTPException(status_code=502, detail="Foundry returned a streaming error.")
        return []

    def interrupt(self, failed: bool) -> None:
        self.record.status = "failed" if failed else "cancelled"
        for activity in self.record.activity:
            if activity.status == "running":
                activity.status = "failed" if failed else "cancelled"
