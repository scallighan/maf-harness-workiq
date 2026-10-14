# Workplace productivity agent: Foundry Toolbox + Work IQ

A Microsoft Foundry Hosted Agent using Microsoft Agent Framework's Agent Harness
and the documented Work IQ Toolbox integration:

- A dedicated Entra application has the delegated `WorkIQAgent.Ask` permission.
- A Foundry project OAuth connection targets Work IQ's A2A endpoint.
- A toolbox contains `WorkIQPreviewToolboxTool` referencing that connection.
- The runtime uses the SDK's existing `FoundryToolbox` directly. No custom MCP
  transport, standalone OAuth broker, shared user token or manual caller binding
  is used by this path.

The model delegates workplace questions to Work IQ, which returns synthesized
Microsoft 365 answers and sources under the authorizing user's permissions.
The agent requests read-only research and must report access/search limitations.
Tenant policy remains responsible for enforcing permitted Work IQ operations.

**This initial Toolbox path does not retrieve PDF bytes or independently verify
page numbers.** It must say "page not verified" rather than inventing page evidence.
The earlier direct endpoint and page-verification code is deferred; historical
design notes are preserved in [docs/standalone.md](docs/standalone.md).

## Prerequisites

- Python 3.12+, uv, Terraform 1.9+, Azure CLI; Docker for a hosted release.
- An Azure subscription with Foundry hosted-agent support and model quota.
- Work IQ tenant enablement, usage billing and the calling user's entitlements.
- Foundry project access for the developer, runtime identity and intended users.
- An appropriately authorized tenant administrator for app creation and delegated
  admin consent. Use PIM where your organization requires it.

Work IQ and Foundry have separate processing/service boundaries. Review tenant
policy, consent, data handling and regional requirements before sending work data.
Sensitive-content tracing remains disabled.

## 1. Provision the platform

From the repository root:

```bash
az login
az account set --subscription YOUR_SUBSCRIPTION_ID
uv sync --frozen --extra dev

# For a new stack only; do not overwrite existing configuration:
cp terraform/terraform.tfvars.sample terraform/terraform.tfvars
# Set subscription_id and review location, model and quota.
terraform -chdir=terraform init
terraform -chdir=terraform plan -out=platform.tfplan
terraform -chdir=terraform apply platform.tfplan
```

Alternatively, load an existing `terraform/.env` with
`set -a; source terraform/.env; set +a`.

Terraform creates the Foundry project, model, guardrail, registry, observability,
Cosmos conversation history and a dedicated Toolbox OAuth application.
Defaults are EastUS2 and `gpt-5.6-luna`; confirm quota and availability.
For `gpt-5.4-mini`, set both `chat_model = "gpt-5.4-mini"` and
`chat_model_version = "2026-03-17"`.

The shared tenant Work IQ service principal is protected against destruction.
If it already exists outside this Terraform state, import it before applying:

```bash
WORKIQ_SP_OBJECT_ID=$(az ad sp show \
  --id fdcc1f02-fc51-4226-8753-f668596af7f7 --query id -o tsv)
terraform -chdir=terraform import azuread_service_principal.workiq "$WORKIQ_SP_OBJECT_ID"
```

Use a secure remote Terraform backend for a team deployment. Review every plan:
the existing networking migration can replace an older non-managed Foundry
account/project. Project endpoints, agent identities and existing conversations
do not automatically migrate.

The Toolbox migration removes the previous broker Container App/environment,
managed identity, Entra broker/demo apps, Key Vault, OAuth token-cache container
and broker-only VNet/private endpoints/DNS. Review those targeted deletions in
the plan. The Foundry project, model, registry, observability, Cosmos chat history
and Foundry-managed Cosmos private endpoint are retained. Key Vault deletion
honors soft-delete/purge protection; its protected soft-deleted vault/key remains
until Azure's retention period expires.

## 2. Configure Work IQ through Foundry Toolbox

Run as the authorized tenant administrator:

```bash
uv run python scripts/setup_toolbox.py
```

The setup script follows the documented Work IQ connection contract:

1. Grants the dedicated application's delegated admin consent.
2. Creates a 90-day client secret in memory and sends it to the Foundry OAuth
   connection. The plaintext secret is never printed, written to a local file or
   put in Terraform state.
3. Adds Foundry's returned OAuth redirect URL to that application and saves only
   the nonsecret redirect URI in git-ignored `terraform/toolbox.auto.tfvars`.
4. Creates the toolbox with `WorkIQPreviewToolboxTool`. Its first version is the
   default automatically.

Repeated setup reuses a matching connection and default toolbox version rather
than issuing another secret or changing a published version. A mismatched
configuration fails explicitly for review. Rotate the client secret before its
90-day lifetime expires and recreate the immutable Foundry connection with the
new credential; rerunning setup does not rotate it.

**Admin consent does not replace each user's OAuth authorization.** Foundry
provides a per-user consent link when the toolbox needs it. Complete that flow as
the intended Microsoft 365 user, then retry. No broker `/login` or caller-ID
mapping is required.

## 3. Run locally

```bash
./scripts/local-test.sh
# Show the configured destinations and turn progress:
./scripts/local-test.sh --verbose
```

The helper loads the Foundry project/model and Toolbox endpoint from Terraform,
syncs locked dependencies and starts `workplace-demo`. It does not apply
Terraform. Azure CLI credentials authenticate to Foundry; Foundry manages the
Work IQ OAuth connection and per-user authorization.

If the Toolbox needs consent, the demo prints its HTTPS authorization link and
exits with a failure status. Open the link as the intended user, complete consent
and rerun the helper. Do not share consent links in logs.

Use `/new` to reset the in-memory conversation and `/quit` to exit. Tool approval
requests, if configured, are shown explicitly: `y` approves and Enter denies.
The decision is submitted on the same session before another message is accepted.

Manual configuration is available through `.env.example`; load its values into
your shell and run `uv run workplace-demo`. The prior `--device-code` and
`--show-tool-errors` options belong to the deferred standalone implementation and
are not part of this Toolbox demo.

Try a read-only workplace question, for example:

> Find recent documents about renewal planning in my OneDrive and email.
> Give me the returned source links, and state any access limitations.

## 4. Deploy the hosted agent

```bash
./scripts/deploy.sh
```

For an existing platform, the script first builds and pushes the Linux/amd64
Toolbox image, persists its immutable digest in `terraform/agent.auto.tfvars`,
then applies the release and broker cleanup together. It configures the Work IQ
toolbox and waits for that exact agent version/image to become active.
For a new stack it provisions the platform before the build as well.
Every Terraform apply requires confirmation.

The hosted factory creates request-owned clients and a `FoundryToolbox`, using
the hosted instance identity for Foundry and Cosmos. The SDK forwards Foundry's
trusted per-request call context to the Toolbox for per-user authorization.
Terraform grants the instance Foundry User on the project and container-scoped
Cosmos history access.

The hosted Responses adapter handles Toolbox OAuth consent requests. Complete
the per-user consent flow when prompted by Foundry and retry. Active deployment
status alone proves neither Work IQ consent nor Microsoft 365 access.

Custom hosted configuration uses `AZURE_AI_PROJECT_ENDPOINT` and
`TOOLBOX_ENDPOINT`; `FOUNDRY_*` and `AGENT_*` variables are reserved for platform
injection. Foundry's own injected endpoint remains available to its SDK.

## Development

```bash
uv run --extra dev python -m pytest -q
uv run --extra dev ruff check .
uv run --extra dev pyright
terraform fmt -check -recursive terraform
terraform -chdir=terraform validate
bash -n scripts/deploy.sh scripts/local-test.sh
```

Offline tests cover the runtime's request-owned resources, local history and
approval flow, OAuth connection setup, credential handling, Toolbox reuse and
deployment scripts. The deferred direct endpoint/evidence tests remain separate.
Live tenant policy, consent, role propagation and service availability must be
checked with the intended user.

## References

- [Work IQ with a hosted agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/work-iq?pivots=python#use-work-iq-with-a-hosted-agent)
- [Use a toolbox with a hosted agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/use-toolbox-hosted-agent)
- [Microsoft Agent Framework Foundry Toolbox](https://learn.microsoft.com/en-us/agent-framework/integrations/by-component/tools/foundry-toolbox)
- [Toolbox authentication](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/tool-authentication)
