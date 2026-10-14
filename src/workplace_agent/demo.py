from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

from agent_framework import Content
from agent_framework.exceptions import AgentFrameworkException
from azure.ai.agentserver.core import AgentConfig
from mcp.shared.exceptions import McpError

from .app import create_agent

logger = logging.getLogger(__name__)


def consent_links(error: AgentFrameworkException) -> list[str]:
    mcp_error = next((arg for arg in error.args if isinstance(arg, McpError)), None)
    if mcp_error is None or mcp_error.error.code != -32006:
        return []
    start = mcp_error.error.message.find("{")
    if start == -1:
        raise RuntimeError("Foundry returned an OAuth consent error without structured details.") from error
    details = json.loads(mcp_error.error.message[start:])
    if not isinstance(details, dict) or not isinstance(details.get("errors"), list):
        raise RuntimeError("Foundry returned invalid OAuth consent details.") from error
    links: list[str] = []
    for item in details["errors"]:
        if not isinstance(item, dict) or not isinstance(item.get("error"), dict):
            raise RuntimeError("Foundry returned invalid OAuth consent details.") from error
        if item["error"].get("code") != "CONSENT_REQUIRED":
            continue
        link = item["error"].get("message")
        if not isinstance(link, str) or any(char.isspace() or ord(char) < 32 for char in link):
            raise RuntimeError("Foundry returned an invalid OAuth consent link.") from error
        parsed = urlsplit(link)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeError("Foundry returned an invalid OAuth consent link.") from error
        links.append(link)
    if not links:
        raise RuntimeError("Foundry requires OAuth consent but did not provide a consent link.") from error
    return list(dict.fromkeys(links))


async def run_demo() -> None:
    if AgentConfig.from_env().is_hosted:
        raise RuntimeError("The interactive demo is local-only; use workplace-agent in Foundry.")
    async with AsyncExitStack() as resources:
        agent = await create_agent()
        try:
            await resources.enter_async_context(agent)
        except AgentFrameworkException as error:
            links = consent_links(error)
            if not links:
                raise
            print("Work IQ authorization is required. Open each Foundry consent link as the intended user:")
            for link in links:
                print(link)
            print("Complete authorization, then rerun the local demo. Do not share these consent links.")
            raise SystemExit(1) from None
        session = agent.create_session()
        print("Foundry Toolbox Work IQ demo. History stays in memory. Type /new to reset, /quit to exit.")
        while True:
            try:
                prompt = (await asyncio.to_thread(input, "\nYou: ")).strip()
            except EOFError:
                return
            if prompt == "/quit":
                return
            if prompt == "/new":
                session = agent.create_session()
                print("Started a new conversation.")
            elif prompt:
                logger.info("Starting agent turn; waiting for model output and Foundry Toolbox calls.")
                messages: str | list[Content] = prompt
                while True:
                    approvals: dict[str, Content] = {}
                    print("\nAssistant: ", end="", flush=True)
                    async for update in agent.run(messages, session=session, stream=True):
                        if update.text:
                            print(update.text, end="", flush=True)
                        for content in update.contents:
                            if content.type == "function_approval_request":
                                if not content.id or content.function_call is None:
                                    raise RuntimeError(
                                        "Tool approval request is missing its ID or function call."
                                    )
                                approvals[content.id] = content
                    print()
                    if not approvals:
                        break
                    messages = []
                    for request_id, approval in approvals.items():
                        function_call = approval.function_call
                        if function_call is None:
                            raise RuntimeError("Tool approval request is missing its function call.")
                        print(
                            f"\nTool approval required: {function_call.name}\n"
                            f"Arguments: {json.dumps(function_call.arguments)}"
                        )
                        while True:
                            try:
                                answer = (
                                    await asyncio.to_thread(input, "Approve this tool call? [y/N]: ")
                                ).strip()
                            except EOFError:
                                return
                            if answer.lower() in ("y", "yes", "n", "no", ""):
                                break
                            print("Enter y to approve or n to deny.")
                        approved = answer.lower() in ("y", "yes")
                        logger.info("Tool approval decision: %s (approved=%s).", function_call.name, approved)
                        messages.append(Content.from_function_approval_response(
                            approved=approved, id=request_id, function_call=function_call,
                        ))
                    logger.info("Resuming agent turn with tool approval responses.")
                logger.info("Agent turn complete.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive Agent Harness demo with Work IQ through Foundry Toolbox."
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show connection, sign-in and tool-call progress without logging sensitive payloads.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("workplace_agent").setLevel(logging.INFO if args.verbose else logging.WARNING)
    asyncio.run(run_demo())


if __name__ == "__main__":
    main()
