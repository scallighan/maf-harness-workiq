# Workplace productivity agent: Foundry Toolbox + Work IQ

A Microsoft Foundry Hosted Agent using Microsoft Agent Framework's Agent Harness
and a custom Work IQ MCP connection exposed through Foundry Toolbox:

- A dedicated Entra application has the delegated `WorkIQAgent.Ask` permission.
- A Terraform-managed custom OAuth connection targets
  `https://workiq.svc.cloud.microsoft/mcp`.
- A toolbox contains a generic `MCPToolboxTool`, not `WorkIQPreviewToolboxTool`.
- Terraform inputs control which upstream tools are exposed and whether calls
  require explicit user approval.
- The runtime uses the SDK's existing `FoundryToolbox` transport. No custom MCP
  transport, standalone OAuth broker, shared user token or manual caller binding
  is used by this path.

The model can call Work IQ's individual MCP tools for source-grounded retrieval, semantic questions,
structured reads, schema discovery and document-byte retrieval under the
authorizing user's permissions.
The agent requests read-only research and must report access/search limitations.
Tenant policy remains responsible for enforcing permitted Work IQ operations.

The packaged `document-finder` skill is loaded through the harness's real
`load_skill` tool on each file-research turn, including follow-ups. It covers all
file types and automatically asks for native source locations: PDF pages,
presentation slides, document sections/paragraphs, spreadsheet sheets/cells,
text/code lines and audio/video timestamps. Missing locations trigger a targeted
read-only follow-up before the answer, rather than requiring another user prompt.
Discovery prefers `retrieve` with the `grounding` strategy across files and email,
then targeted/batched `fetch` reads of returned sources. Known files do not trigger
another global search. `ask` is reserved for relevant evidence/location gaps or
retrieval limitations, avoiding an unnecessary second answer-generation pass.
When a retrieval requests both files and email, a thin SDK subclass fans out the
two source-scoped requests concurrently on separate request-owned Toolbox sessions.
This preserves the SDK's caller-header isolation lock instead of bypassing it.
Results remain tagged by source, including original metadata and local citation IDs,
for the agent to merge and deduplicate without losing distinct findings. Narrowed
or other source selections retain the normal SDK behavior. Approval, credentials
and the upstream connection remain unchanged; failures are surfaced, not hidden.
`search_paths` discovers supported API operations, not document content; direct
drive search is used only with discovered paths. Different retrieval paths do not
guarantee identical rankings or complete results, so relevance, attachment coverage
and citation checks still apply. The advertised `retrieve` tool must be available
in the tenant; existing `WorkIQAgent.Ask` delegated consent is reused.

**This path does not independently verify source locations.** Exposing
`fetch_blob` alone does not verify pages, slides or other locators. The agent labels
source-returned positions as reported and says "page not verified", "slide not
verified", or the native-location equivalent instead of inventing evidence.
The earlier direct endpoint and page-verification code is deferred; historical
design notes are preserved in [docs/standalone.md](docs/standalone.md).

## Prerequisites

- Python 3.12+, uv, Terraform 1.11+, Azure CLI; Docker for a hosted release.
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
Cosmos conversation history, a dedicated Toolbox OAuth application and the
custom Work IQ MCP connection.
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

## 2. Manage the custom Work IQ MCP connection

If `work-iq-mcp` already exists, import it before applying. This preserves the
OAuth client secret already stored by Foundry. Azure requires that same secret
to be resubmitted for any connection update; importing alone does not rotate it:

```bash
PROJECT_ID=$(terraform -chdir=terraform output -raw foundry_project_id)
terraform -chdir=terraform import azapi_resource.workiq_mcp \
  "$PROJECT_ID/connections/work-iq-mcp?api-version=2025-06-01"
terraform -chdir=terraform plan
terraform -chdir=terraform apply
```

For a new connection, first create a secret on the dedicated application
(`workiq_toolbox_client_id`), provide it through the ephemeral environment
variable `TF_VAR_workiq_mcp_client_secret`, and set
`TF_VAR_workiq_mcp_secret_version=1` when applying. Use the same process with an
incremented version for explicit rotation. Do not put the secret in a tfvars
file, command-line argument or source control. AzAPI's write-only
`sensitive_body` keeps it out of Terraform plans and state. Unset the secret
environment variable after applying; keep the nonsecret version in your
Terraform configuration. An unchanged imported connection needs no secret
input. For adoption updates or later connection changes, provide the existing
secret again and increment the credential version so AzAPI sends it with the
update. A version bump alone does not generate or rotate an application secret.
On a brand-new stack, bootstrap the application first with
`terraform -chdir=terraform apply -target=azuread_service_principal.toolbox`,
create its secret, then run the full platform apply with the ephemeral input.

Configure interaction controls in your Terraform inputs:

```hcl
workiq_mcp_allowed_tools    = ["ask", "fetch", "get_schema", "search_paths", "fetch_blob", "retrieve"]
workiq_mcp_require_approval = "never"
```

The default allowlist excludes create/update/delete/action tools. Use exact
upstream names, not the toolbox-prefixed names. The default `never` permits calls
without a per-call prompt. `always` publishes approval metadata, but the hosted
Toolbox runtime does not currently enforce that metadata as a confirmation gate;
do not rely on it for approval enforcement. Apply the
Terraform configuration and run setup to publish changed controls.

Run as the authorized tenant administrator:

```bash
uv run python scripts/setup_toolbox.py
```

The setup script uses the Terraform-managed connection:

1. Grants the dedicated application's delegated admin consent.
2. Reads and validates the existing custom MCP connection. It never creates
   a connection, issues a secret or rotates stored credentials.
3. Adds Foundry's returned OAuth redirect URL to that application and saves only
   the nonsecret redirect URI in git-ignored `terraform/toolbox.auto.tfvars`.
4. Publishes `MCPToolboxTool` with the Terraform allowlist and approval policy.
   Its first version is the default automatically; later changed configurations
   create a new immutable version and explicitly promote it.

Repeated setup reuses a matching default toolbox version. It preserves existing
OAuth redirects, including callbacks added by Foundry consent. Manage the secret's
expiry separately; rerunning setup does not extend or rotate it. The earlier
preview A2A connection is not used by the new toolbox; retain it during migration
until the generic MCP path has been verified.

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
then applies the release and custom MCP connection configuration. It configures the Work IQ
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

## 5. Deploy the chat UI

On the existing platform, load the same Terraform environment and run:

```bash
set -a; source terraform/.env; set +a
./scripts/deploy_ui.sh
```

The script builds the UI/API container, pins its digest in git-ignored
`terraform/ui.auto.tfvars`, and asks for Terraform confirmation. Run it with an
administrator authorized to consent to the UI's delegated permissions. It grants
consent to the dedicated browser/API applications, then checks `/readyz`.
The UI URL is also available from `terraform output -raw ui_url`.

The optional UI uses a VNet-injected Container Apps consumption environment with
a dedicated Cosmos SQL private endpoint and private DNS. It reuses the existing
registry, observability workspace and private Cosmos account; it does not enable
Cosmos public access or key authentication. Its managed identity can access only
the `uiSessions` container. `ui_min_replicas = 0` scales to zero when idle; use `1`
if cold starts are undesirable.

Sign in with an authorized Foundry/Microsoft 365 account. The API validates the
browser's delegated token and exchanges it on behalf of that same user for
Foundry and Graph access. A managed-identity federated credential authenticates
the exchange without an application secret. Each user has a separate session
catalog; Foundry stores conversation transcripts and Cosmos stores session
metadata and per-prompt activity. Tenant admin consent does not grant Foundry
project access or replace individual Work IQ OAuth authorization.
Conversation creation, history and deletion use the hosted agent's OpenAI
endpoint, the same scope as its Responses API. Empty sessions created with the
earlier project-level endpoint are upgraded on their next prompt without changing
their session IDs or activity records. Sessions with existing project-level
history are retained and require a new session rather than silently discarding it.
The browser application registers `/auth.html` as a dedicated same-origin MSAL
redirect bridge for both popup sign-in and sign-out. If an abandoned popup leaves
an `interaction_in_progress` error, close that window and use **Retry sign-in**;
the UI never automatically overrides an active authentication interaction.

The sidebar supports creating, selecting, renaming and deleting sessions. Signing
in with an empty catalog automatically creates and selects the first session.
Each prompt shows actual tool calls and explicitly reported skill invocations
from the hosted stream, with a visible progress indicator before the first call.
The activity summary separates skill/tool counts; a successful `load_skill` call
shows **Skill loaded** and the skill name. This confirms instruction loading,
not independently that every instruction was followed or a citation verified.
New calls carry execution durations measured inside the agent, not the gap between
receiving streamed call/result events (which may be batched). These durations
exclude model time and delay before tool invocation. Sub-second calls show milliseconds; historical calls
without runtime timing show **Timing unavailable**, not a misleading `0.0s`.
Expandable **Input / output** controls load the selected call from its stored
Foundry response after the prompt finishes. Payloads remain in Foundry rather
than being copied into Cosmos; credential fields and authentication/temporary
URLs are redacted and large payloads are truncated. Private reasoning is excluded.
Answers render GitHub-flavored Markdown, including tables; raw HTML and external
image loading remain disabled.

Source cards open a delegated Graph preview using drive/item IDs, or resolve
SharePoint file URLs and `AllItems.aspx?id=...` library links to those IDs.
The API application requires delegated `Files.Read` and `Sites.Read.All`; both
are read-only and effective access remains limited to the signed-in user.
Unsupported links retain **Open original** rather than guessing file identifiers
or fetching arbitrary URLs. Page/slide positions are
best-effort viewer hints and always marked **not verified**. Preview URLs are
short-lived and are not stored in Cosmos.

For local frontend development, add `http://localhost:5173/` to
`ui_additional_redirect_uris`, apply, and use `npm run dev` in `ui/`.
Vite proxies `/api` to `UI_API_PROXY` (default `http://127.0.0.1:8080`).
The API requires the deployed Entra/Cosmos configuration and an attached managed
identity; there is no local authentication bypass. Standard Container Apps HTTP
ingress has a 240-second request limit; the bridge sends keep-alive comments but
does not promise unlimited-duration prompts.

## Development

```bash
uv sync --frozen --extra dev --extra ui
uv run python -m pytest -q
uv run ruff check .
uv run pyright
npm --prefix ui ci
npm --prefix ui run build
# Run frontend tests with Node 24+ (the container build uses Node 24).
npm --prefix ui test
terraform fmt -check -recursive terraform
terraform -chdir=terraform validate
bash -n scripts/deploy.sh scripts/deploy_ui.sh scripts/local-test.sh
```

Offline tests cover the runtime's request-owned resources, local history and
approval flow, Terraform connection validation, callback preservation, generic
MCP Toolbox publication/reuse and
deployment scripts. The deferred direct endpoint/evidence tests remain separate.
Live tenant policy, consent, role propagation and service availability must be
checked with the intended user.

## References

- [Work IQ with a hosted agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/work-iq?pivots=python#use-work-iq-with-a-hosted-agent)
- [Use a toolbox with a hosted agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/use-toolbox-hosted-agent)
- [Microsoft Agent Framework Foundry Toolbox](https://learn.microsoft.com/en-us/agent-framework/integrations/by-component/tools/foundry-toolbox)
- [Toolbox authentication](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/tool-authentication)
- [Work IQ MCP overview](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/overview)
