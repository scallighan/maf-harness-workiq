import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

OUTPUTS = {
    "foundry_project_endpoint": "https://example.services.ai.azure.com/api/projects/workplace",
    "chat_model_deployment": "test-model",
    "tenant_id": "test-tenant",
    "workiq_toolbox_endpoint": "https://example.services.ai.azure.com/api/projects/workplace/toolboxes/work-iq/mcp",
}
ENVIRONMENT = {
    "FOUNDRY_PROJECT_ENDPOINT": OUTPUTS["foundry_project_endpoint"],
    "AZURE_AI_PROJECT_ENDPOINT": OUTPUTS["foundry_project_endpoint"],
    "AZURE_AI_MODEL_DEPLOYMENT_NAME": OUTPUTS["chat_model_deployment"],
    "AZURE_TENANT_ID": OUTPUTS["tenant_id"],
    "TOOLBOX_ENDPOINT": OUTPUTS["workiq_toolbox_endpoint"],
    "ENABLE_SENSITIVE_DATA": "false",
}


@pytest.fixture
def local_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "scripts/local-test.sh", scripts / "local-test.sh")
    tools = tmp_path / "bin"
    tools.mkdir()
    mock = f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["LOCAL_DEMO_LOG"], "a") as log:
    log.write(json.dumps({{"tool": name, "args": args, "cwd": os.getcwd()}}) + "\\n")
stage = "login" if name == "az" else "output" if name == "terraform" else args[0]
if os.environ.get("FAIL_STAGE") == stage:
    print("Mock failure: " + stage, file=sys.stderr)
    sys.exit(7)
if name == "terraform":
    print({OUTPUTS!r}[args[3]])
elif name == "uv" and args[:2] == ["run", "workplace-demo"]:
    print(json.dumps({{key: os.environ.get(key) for key in {list(ENVIRONMENT)!r}}}))
"""
    for name in ["terraform", "az", "uv"]:
        executable = tools / name
        executable.write_text(mock)
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("LOCAL_DEMO_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setenv("ENABLE_SENSITIVE_DATA", "true")
    monkeypatch.delenv("FAIL_STAGE", raising=False)
    return tmp_path


@pytest.mark.parametrize("args", [[], ["--verbose"]])
def test_local_demo_loads_outputs_and_forwards_arguments(local_demo: Path, args: list[str]) -> None:
    result = subprocess.run(
        ["bash", str(local_demo / "scripts/local-test.sh"), *args],
        cwd=local_demo.parent, check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == ENVIRONMENT
    calls = [json.loads(line) for line in (local_demo / "calls.log").read_text().splitlines()]
    assert all(call["cwd"] == str(local_demo) for call in calls)
    assert calls[-2]["args"] == ["sync", "--frozen"]
    assert calls[-1]["args"] == ["run", "workplace-demo", *args]
    assert all(call["args"][1:3] == ["output", "-raw"] for call in calls if call["tool"] == "terraform")


@pytest.mark.parametrize("stage", ["login", "output", "sync", "run"])
def test_local_demo_propagates_failures(
    local_demo: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    monkeypatch.setenv("FAIL_STAGE", stage)
    result = subprocess.run(
        ["bash", str(local_demo / "scripts/local-test.sh")], capture_output=True, text=True,
    )
    assert result.returncode == (1 if stage == "login" else 7)
    assert f"Mock failure: {stage}" in result.stderr
    calls = [json.loads(line) for line in (local_demo / "calls.log").read_text().splitlines()]
    if stage != "run":
        assert not any(call["args"][:2] == ["run", "workplace-demo"] for call in calls)
    if stage == "login":
        assert "az login" in result.stderr
