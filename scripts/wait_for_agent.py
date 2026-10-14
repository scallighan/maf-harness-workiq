"""Wait for an exact hosted agent version to be active and verify its immutable image."""

import argparse
import time

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import HostedAgentDefinition
from azure.core.exceptions import HttpResponseError
from azure.identity import AzureCliCredential


def wait_for_agent(
    project: AIProjectClient, name: str, version: str, image: str, *, timeout: int = 600,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            candidate = project.agents.get_version(
                agent_name=name, agent_version=version,
                connection_timeout=min(30, remaining), read_timeout=min(30, remaining),
                retry_total=0,
            )
        except HttpResponseError as exc:
            if exc.status_code not in {408, 429, 500, 502, 503, 504}:
                raise
            print(f"Transient status request failure (HTTP {exc.status_code}); retrying.", flush=True)
        else:
            if candidate.status == "active":
                definition = candidate.definition
                if (
                    not isinstance(definition, HostedAgentDefinition)
                    or definition.container_configuration is None
                    or definition.container_configuration.image != image
                ):
                    raise RuntimeError("Active candidate does not match the expected release image.")
                print(f"{name}:{version} is active; image matches.", flush=True)
                return
            if candidate.status != "creating":
                raise RuntimeError(f"Candidate version has unexpected/terminal status: {candidate.status}")
            print(f"{name}:{version} is creating.", flush=True)
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    raise TimeoutError("Timed out waiting for the candidate version to become active.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive.")
    with (
        AzureCliCredential() as credential,
        AIProjectClient(endpoint=args.project_endpoint, credential=credential) as project,
    ):
        wait_for_agent(project, args.name, args.version, args.image, timeout=args.timeout)


if __name__ == "__main__":
    main()
