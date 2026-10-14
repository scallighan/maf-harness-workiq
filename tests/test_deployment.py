import runpy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from azure.ai.projects.models import ContainerConfiguration, HostedAgentDefinition
from azure.core.exceptions import HttpResponseError


def load_waiter():
    return runpy.run_path("scripts/wait_for_agent.py")["wait_for_agent"]


def candidate(status: str, image: str = "registry.azurecr.io/agent@sha256:expected"):
    return SimpleNamespace(
        status=status,
        definition=HostedAgentDefinition(
            cpu="1", memory="2Gi",
            container_configuration=ContainerConfiguration(image=image),
        ),
    )


def test_waiter_checks_exact_version_and_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    project = MagicMock()
    project.agents.get_version.side_effect = [
        candidate("creating"),
        candidate("active"),
    ]
    load_waiter()(project, "workplace", "42", "registry.azurecr.io/agent@sha256:expected")
    assert project.agents.get_version.call_count == 2
    for call in project.agents.get_version.call_args_list:
        assert call.kwargs["agent_name"] == "workplace"
        assert call.kwargs["agent_version"] == "42"


@pytest.mark.parametrize("status,image", [
    ("active", "registry.azurecr.io/agent@sha256:wrong"),
    ("failed", "registry.azurecr.io/agent@sha256:expected"),
    ("deleted", "registry.azurecr.io/agent@sha256:expected"),
])
def test_waiter_rejects_failed_or_wrong_release(status: str, image: str) -> None:
    project = MagicMock()
    project.agents.get_version.return_value = candidate(status, image)
    with pytest.raises(RuntimeError):
        load_waiter()(project, "workplace", "42", "registry.azurecr.io/agent@sha256:expected")


def test_waiter_does_not_hide_authorization_failure() -> None:
    project = MagicMock()
    error = HttpResponseError("Forbidden")
    error.status_code = 403
    project.agents.get_version.side_effect = error
    with pytest.raises(HttpResponseError, match="Forbidden"):
        load_waiter()(project, "workplace", "42", "registry.azurecr.io/agent@sha256:expected")
