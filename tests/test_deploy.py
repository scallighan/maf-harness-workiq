import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = "https://example.services.ai.azure.com/api/projects/workplace"
IMAGE = f"example.azurecr.io/workplace-agent@sha256:{'a' * 64}"


def test_hosted_agent_environment_does_not_override_platform_variables() -> None:
    definition = (Path(__file__).parents[1] / "terraform/agent.tf").read_text()
    environment = re.search(
        r"environment_variables\s*=\s*\{(.*?)^\s*\}", definition, re.MULTILINE | re.DOTALL,
    )
    assert environment is not None
    names = re.findall(r"^\s*(\w+)\s*=", environment.group(1), re.MULTILINE)
    assert names
    assert not [name for name in names if name.startswith(("FOUNDRY_", "AGENT_"))]


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (tmp_path / "terraform").mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "scripts/deploy.sh", scripts / "deploy.sh")
    shutil.copyfile(Path(__file__).parents[1] / "scripts/deploy_ui.sh", scripts / "deploy_ui.sh")
    tools = tmp_path / "bin"
    tools.mkdir()
    mock = f"""#!{sys.executable}
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["DEPLOY_LOG"], "a") as log:
    log.write(name + " " + " ".join(args) + "\\n")
if name == "terraform" and args[1:3] == ["output", "-json"]:
    print("{{}}" if os.environ.get("NO_PLATFORM") else '{{"registry_name":{{"value":"example"}}}}')
elif name == "terraform" and args[1:3] == ["output", "-raw"]:
    print({{
        "foundry_project_endpoint": {PROJECT!r},
        "registry_name": "example",
        "subscription_id": "subscription-id",
        "registry_login_server": "example.azurecr.io",
        "agent_name": "workplace-productivity",
        "agent_version": "4",
        "workiq_broker_url": "https://broker.example.com",
        "ui_url": "https://ui.example.com",
        "ui_api_client_id": "api-client-id",
        "ui_browser_client_id": "browser-client-id",
    }}[args[3]])
elif name == "az" and args[:3] == ["acr", "repository", "show"]:
    print("sha256:" + "a" * 64)
elif name == "docker" and args[0] == "push" and os.environ.get("FAIL_PUSH"):
    sys.exit(7)
elif name == "uv" and args[:2] == ["run", "python"] and args[2] in ("-", "-c"):
    os.execv(sys.executable, [sys.executable, *args[2:]])
elif (name == "uv" and args[:3] == ["run", "python", "scripts/setup_toolbox.py"]
      and os.environ.get("FAIL_TOOLBOX")):
    sys.exit(22)
"""
    for name in ["terraform", "az", "docker", "uv", "curl"]:
        executable = tools / name
        executable.write_text(mock)
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("DEPLOY_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setenv("TAG", "test-release")
    for name in ["FAIL_PUSH", "FAIL_TOOLBOX", "NO_PLATFORM"]:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_deploy_pins_release_and_waits_for_exact_image(deployment: Path) -> None:
    subprocess.run(["bash", str(deployment / "scripts/deploy.sh")], check=True, capture_output=True)
    release = (deployment / "terraform/agent.auto.tfvars").read_text().splitlines()
    assert json.loads(release[0].split(" = ", 1)[1]) == IMAGE
    assert len(release) == 1
    calls = (deployment / "calls.log").read_text()
    assert calls.count("terraform -chdir=terraform apply\n") == 1
    assert calls.index("docker push") < calls.index("terraform -chdir=terraform apply\n")
    assert "uv run python scripts/setup_toolbox.py" in calls
    assert f"--name workplace-productivity --version 4 --image {IMAGE}" in calls
    assert "https://broker.example.com/readiness" not in calls


def test_deploy_fails_when_toolbox_setup_fails(
    deployment: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAIL_TOOLBOX", "1")
    result = subprocess.run(
        ["bash", str(deployment / "scripts/deploy.sh")], capture_output=True, text=True,
    )
    assert result.returncode == 22
    assert "scripts/wait_for_agent.py" not in (deployment / "calls.log").read_text()


def test_deploy_bootstraps_an_unprovisioned_platform(
    deployment: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PLATFORM", "1")
    subprocess.run(["bash", str(deployment / "scripts/deploy.sh")], check=True, capture_output=True)
    calls = (deployment / "calls.log").read_text()
    assert calls.count("terraform -chdir=terraform apply\n") == 2
    assert calls.index("terraform -chdir=terraform apply\n") < calls.index("docker build")


def test_failed_push_preserves_previous_release_inputs(
    deployment: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAIL_PUSH", "1")
    release = deployment / "terraform/agent.auto.tfvars"
    release.write_text("previous release\n")
    result = subprocess.run(["bash", str(deployment / "scripts/deploy.sh")], capture_output=True)
    assert result.returncode == 7
    assert release.read_text() == "previous release\n"
    assert "scripts/wait_for_agent.py" not in (deployment / "calls.log").read_text()


def test_ui_deploy_pins_image_configures_consent_and_checks_private_storage(deployment: Path) -> None:
    subprocess.run(["bash", str(deployment / "scripts/deploy_ui.sh")], check=True, capture_output=True)
    release = (deployment / "terraform/ui.auto.tfvars").read_text().splitlines()
    assert json.loads(release[0].split(" = ", 1)[1]) == IMAGE.replace("workplace-agent@", "workplace-ui@")
    calls = (deployment / "calls.log").read_text()
    assert "docker build --platform linux/amd64 -f ui/Dockerfile" in calls
    assert calls.index("docker push") < calls.index("terraform -chdir=terraform apply\n")
    assert "uv run python -m scripts.setup_ui" in calls
    assert "https://ui.example.com/readyz" in calls
    assert "-auto-approve" not in calls


def test_failed_ui_push_preserves_previous_release(
    deployment: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAIL_PUSH", "1")
    release = deployment / "terraform/ui.auto.tfvars"
    release.write_text("previous UI release\n")
    result = subprocess.run(["bash", str(deployment / "scripts/deploy_ui.sh")], capture_output=True)
    assert result.returncode == 7
    assert release.read_text() == "previous UI release\n"
    assert "terraform -chdir=terraform apply\n" not in (deployment / "calls.log").read_text()
