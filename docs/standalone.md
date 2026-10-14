# Deferred standalone Work IQ implementation

These notes describe the earlier direct-MCP implementation, retained for future
work. The active runtime now uses the documented Foundry Toolbox integration;
see the repository README for current setup and commands. The direct OAuth broker,
raw-document tooling and page-verification workflow below are not in the active
agent path. Historical local-demo and deployment commands below no longer select
the direct implementation.

A Microsoft Foundry **Hosted Agent** built with the Microsoft Agent Framework
**Agent Harness**, inspired by [azure-travel-concierge](https://github.com/scallighan/azure-travel-concierge).
It finds workplace documents across **OneDrive/SharePoint and email attachments**,
then retrieves document bytes to verify quotations and page numbers.
Both the interactive demo and hosted agent call **Work IQ MCP directly**:
there is no Foundry Toolbox, project tool connection, or Toolbox publication step.

This is a deployable sample, not an already-provisioned service. Live use requires
Azure quota, Work IQ billing/entitlements, tenant approval, and delegated OAuth consent.
The hosting and Cosmos history packages include preview features; the Python and
Terraform lockfiles pin the locally validated dependencies.

## What "exactly where" means

For a matching PDF, the evidence tool returns its filename, authoritative source URL,
OneDrive parent path or email subject/sender/received date, **one-based PDF viewer page**,
printed page label, a page-local quote, and a SHA-256 fingerprint of the retrieved bytes.
Email URLs open the message, not the attachment: the answer tells the user which
attachment to open. It does not invent attachment deep links.

| Source | Verified location support |
| --- | --- |
| OneDrive/SharePoint PDF | Original PDF page and quote, plus source file URL and available folder metadata |
| OneDrive Office document | Requests Work IQ's PDF conversion; cites **page N of the PDF rendition**, not native Word layout |
| PDF email file attachment | Original PDF page and quote, plus message link and attachment filename |
| Office email attachment / reference attachment | No automatic conversion or upload. Follow an existing reference to OneDrive, or give a verified source/section with **page not verified** |
| Scanned, encrypted, oversized or inaccessible document | Explicit limitation; no fabricated page numbers. OCR is not included |

Discovery answers are leads, not proof: a page citation requires a nonempty match
from `inspect_document`. Matches are literal and case-insensitive, allowing whitespace
differences, not a semantic proof of every statement in the answer.
Limits are 4 MiB per returned PDF, 500 pages, and 12 returned matches.
Partially scanned PDFs report that textless pages were not searched.
Work IQ path availability, conversion formats, collection paging, labels and
permissions depend on your tenant; incomplete search scope must be reported.

Example request:

> Find the document stating the renewal deadline. Search my OneDrive and email
> attachments. Give me the exact quote, link, folder or email details, and page.

An answer should look like this **only when supported by returned evidence**:

> **Renewal plan.pdf**, PDF viewer page 2 (printed label ii):
> "The renewal deadline is October 30."
> Open the linked email "Renewal" from the returned sender/date, then open
> the attachment **Renewal plan.pdf** and go to page 2.

## Architecture and identity

```text
Local terminal -> Agent Harness + in-memory history -> Foundry model
                      |
                      +-> direct Work IQ MCP, signed-in user's delegated token

Signed-in user -> Foundry Responses API -> request-owned Agent Harness -> Foundry model
                                              |
                                              +-> Cosmos chatHistory
                                              +-> direct Work IQ MCP
                                                     ^
                                                     | user's delegated token
                                                OAuth broker (Container Apps)
                                                     |
                                          encrypted Cosmos oauthCache + Key Vault
```

Local Azure CLI credentials authenticate **only to the Foundry model**.
Work IQ uses a separate public-client browser/device sign-in and an in-memory MSAL
cache. The demo retains one conversation until `/new` or exit; switching Work IQ
accounts requires restarting the process so another user cannot inherit its history.
It does not use the hosted broker or Cosmos.

The hosted runtime uses its **agent instance managed identity** for Foundry,
Cosmos history, and the broker's application API. Managed identity is **not**
Microsoft 365 delegated authentication. The broker obtains each user's Work IQ
token through that user's browser authorization, and silently refreshes it when
possible. Work IQ requires the delegated scope
`api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask`; application-only access is unsupported.

Foundry's trusted `x-agent-user-id` is an **opaque platform caller identifier**,
not a guaranteed Entra object ID. An administrator must map it to the authorized
tenant user's object ID using `foundry_user_bindings`. The broker never lets the
browser, model, or request payload register that mapping. Unbound callers and users
without current consent fail explicitly; no shared-account fallback exists.
Only the hosted identity with the `WorkIQ.Token.Read` application role can call
`POST /token`. The broker validates token signature, audience, issuer, tenant,
application identity and role; user/delegated bearer tokens are rejected.
Do not grant that application role to untrusted services.

The broker authenticates its OAuth client using
[managed-identity federation](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-config-app-trust-managed-identity),
not an app secret. OAuth flows and MSAL token caches are encrypted with per-document
AES-GCM keys, wrapped by a nonexportable Key Vault RSA key. Cosmos conditional
writes serialize refreshes across replicas and make browser callbacks single-use.
The hosted agent cannot read the token container or unwrap its encryption keys.
Public broker routes are `/login`, `/callback`, and `/readiness`; token retrieval
always requires the authorized application token.

Hosted conversation messages, tool results and verified citations use the official
`CosmosHistoryProvider`, scoped by tenant, project, trusted caller, platform session
and conversation. History loads from Cosmos rather than a second copy of server
history. Its TTL defaults to 30 days; deletion is asynchronous. Foundry's outer
Responses/session records have separate, platform-managed retention.
Original PDF bytes are not persisted by this sample. OAuth flows expire after
10 minutes; cached authorizations expire after 30 days without a cache refresh.
The broker disables access logging so authorization codes are not written to URL logs.

The remote allowlist is `ask`, `fetch`, `get_schema`, `search_paths`, `fetch_blob`.
No write/action tools, shell, public web search, or document file-memory tools are
registered. Raw Base64 and preauthenticated download URLs are withheld from
model-visible tool results. Retrieved content is treated as untrusted data.
Quotes and metadata still go to the Foundry model: review your organization's
data handling, sensitivity-label and region policies. Work IQ's service terms and
processing boundaries are separate from the container's Azure region.
Sensitive-content tracing is disabled.

## Prerequisites

- Python 3.12+, [uv](https://docs.astral.sh/uv/), Terraform 1.9+, Azure CLI, Docker and curl.
- An Azure subscription with hosted-agent support and model quota in the selected
  region. Defaults are `EastUS2`, `gpt-5.6-luna` version `2026-07-09`,
  `GlobalStandard`, capacity 50; confirm regional availability and approved
  GPT-5.6 quota before applying.
- Permissions to create Azure resources and assign roles, plus Entra permissions
  to create applications, service principals and federated credentials.
- A Global Administrator for the one-time Work IQ service-principal setup and
  delegated admin consent, as required by
  [Microsoft's tenant-enablement guide](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/enable-work-iq).
- Work IQ API billing, entitlements and tenant enablement, including permission
  for read paths and `fetch_blob`. Confirm current requirements in the
  [Work IQ overview](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/overview)
  and [permissions guide](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/permissions).
  Terraform provisions the Work IQ tenant service principal; it does not create
  or impersonate Microsoft's published API application.

## 1. Provision with Terraform

Run from the repository root:

```bash
az login
az account set --subscription YOUR_SUBSCRIPTION_ID
uv sync --frozen --extra dev
cp terraform/terraform.tfvars.sample terraform/terraform.tfvars
# Set subscription_id; review location, model quota, user_object_ids and history_retention_days.
terraform -chdir=terraform init
terraform -chdir=terraform plan -out=platform.tfplan
terraform -chdir=terraform apply platform.tfplan
```

To use `gpt-5.4-mini` instead, set **both** model overrides in `terraform.tfvars`:

```hcl
chat_model         = "gpt-5.4-mini"
chat_model_version = "2026-03-17"
```

These versions follow the
[Azure model catalog](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure).
The local demo reads the deployment name from Terraform's `chat_model_deployment`
output; if editing `.env` directly, keep its model deployment name consistent.

Terraform creates the tenant-local enterprise service principal for Microsoft's
published Work IQ resource (`api://workiq.svc.cloud.microsoft`, app ID
`fdcc1f02-fc51-4226-8753-f668596af7f7`) and configures the documented
`WorkIQAgent.Ask` delegated scope on both client apps. A Global Administrator
must run this first apply. Terraform protects this tenant-wide service principal
from accidental destruction. If it was already provisioned manually or by
another stack, import it before planning:

```bash
WORKIQ_SP_OBJECT_ID=$(az ad sp show --id fdcc1f02-fc51-4226-8753-f668596af7f7 --query id -o tsv)
terraform -chdir=terraform import azuread_service_principal.workiq "$WORKIQ_SP_OBJECT_ID"
```

Alternatively, copy `terraform/env.sample` to `terraform/.env`, edit it, and load
it with `set -a; source terraform/.env; set +a`.

The single `terraform/` root follows the travel-concierge sample's naming,
shared workload tags and platform/release layout. It provisions Foundry account,
project, model, blocking guardrail, ACR, observability, serverless Cosmos,
the broker's Container Apps environment, Key Vault, and both Entra client apps.
The broker uses a VNet-integrated Consumption workload profile and reaches
Key Vault and Cosmos through private endpoints and linked private DNS zones.
Foundry uses a separate Microsoft-managed VNet with a managed `Sql` private
endpoint to the same Cosmos account. Cosmos public access and local authentication
are disabled; the two identities retain their separate container-scoped access.
The managed network uses `AllowInternetOutbound`, so Work IQ, Entra authentication,
the broker, and other public HTTPS dependencies remain reachable. This does not
make Cosmos publicly accessible. `AllowOnlyApprovedOutbound` requires a separate
egress allowlist and can incur managed Azure Firewall charges.
`agent.tf` manages the hosted agent's data-plane resource; `cosmos.tf` and
`broker.tf` own the history and direct-auth resources.
Without `agent_image_uri`, no agent or broker container is registered yet.
App registrations, redirect URI, storage, keys and consent configuration are
available before the first image release.

The project identity gets model/registry access. The **hosted instance identity**,
exported by the agent resource, gets history-container access and the broker API
role after agent creation. The broker identity gets only token-container access,
Key Vault cryptographic access and image pull access. `user_object_ids` grants
project access, **not** Microsoft 365 document access.

Use a secure remote Terraform backend before team deployment. OAuth secrets and
user tokens never enter Terraform state, but state contains identity configuration
and must still be protected. Foundry remains Entra-only (`disableLocalAuth = true`).
The broker's HTTPS sign-in endpoint remains public for browser callbacks and
Foundry access; Key Vault has public access disabled and a default-deny firewall,
with the trusted-Azure-services exception enabled for ARM key provisioning.
Trusted services still require Entra authentication and RBAC permissions; this
exception does not allow direct Azure CLI access or arbitrary application code.
The broker continues to access the vault through its private endpoint.
Its RSA key encrypts token-cache data, not Foundry authentication.
Terraform provisions that key using ARM's `CreateIfNotExist` operation and passes
its **versioned Key Vault URL**, not its ARM ID, to the managed-identity broker.
The key is retained when the provisioning action is removed; deleting the vault
governs its eventual removal under soft-delete/purge-protection settings.
With this exception, Terraform does not need access to the vault's private data
endpoint. ARM key provisioning is **not** exempt from the vault's network
restrictions: setting `bypass = "None"` also blocks this operation with
`ForbiddenByConnection`. If policy requires strictly private-only access, provision
the key through the data plane from a runner connected to the private endpoint
instead of using this ARM action.
Foundry's Entra-authenticated API remains public: outbound managed networking
does not make its inbound endpoint private. ACR and observability also retain
their existing network settings; subscription policies may require further changes.

Managed networking must be set when the Foundry account is first created.
The `-managed` account-name suffix intentionally replaces an earlier account
without network injection, along with its project, model, connections, and scoped
role assignments. Review the destructive migration plan before applying: existing
Foundry conversations, agent identities, and project endpoints do not migrate.
Cosmos containers and broker infrastructure are retained, but history is scoped
to the project, so old conversation history does not automatically carry over.
Reload the new Terraform endpoint output into local environments after migration,
redeploy any hosted agent, and review caller bindings for the new project.
Managed networking cannot be disabled after enabling it. Private endpoint
approval uses the Foundry account identity's Network Connection Approver role,
scoped to Foundry and Cosmos; this role grants no Cosmos document access.

An existing Container Apps environment without VNet integration must be
**replaced** to attach the VNet. The managed infrastructure resource-group name
and Consumption profile counts match Azure's assigned defaults, avoiding repeat
replacement plans once integration is complete.
Review that replacement and the updated broker redirect URI in a fresh plan;
do not reuse a plan saved before these networking changes. The network uses
`10.42.0.0/16`; review address overlap before adding peering.
If an earlier deployment successfully tracked `azurerm_key_vault_key.tokens`,
remove **only that key's Terraform state entry** before planning this migration:
`terraform -chdir=terraform state rm azurerm_key_vault_key.tokens`.
Do not delete or recreate the key; ARM reuses it. A failed first key creation
with no state entry needs no migration.

Foundry account/project/model/connection writes are ordered to avoid overlapping
account operations, with bounded retries only for "another operation is in
progress" conflicts. `ForbiddenByConnection` is a network/policy issue, not RBAC
propagation; do not re-enable public vault access against an enforced policy.
Other RBAC/federation 403s can be transient: verify identity and scope, allow
propagation, and reapply if needed. Do not add broad roles or bypass authorization.

## 2. Grant Work IQ consent

Terraform configures the delegated permission on two dedicated applications.
It does **not** approve tenant-wide consent on your behalf.
The service principal itself is now created by the Terraform platform apply above.

```bash
DEMO_CLIENT_ID=$(terraform -chdir=terraform output -raw workiq_demo_client_id)
BROKER_CLIENT_ID=$(terraform -chdir=terraform output -raw workiq_broker_client_id)
# Run as an appropriately authorized tenant administrator, or use the Entra portal:
az ad app permission admin-consent --id "$DEMO_CLIENT_ID"
az ad app permission admin-consent --id "$BROKER_CLIENT_ID"
```

The public demo app has a `http://localhost` redirect and supports device login.
The broker's Web redirect is its exact HTTPS `/callback` URL; Terraform creates
its managed-identity federated credential. No client secret, Toolbox connection,
or manually entered refresh token is needed.

## 3. Run the direct local demo

With the Terraform platform already provisioned and Azure CLI signed in:

```bash
./scripts/local-test.sh
# On a browserless machine, if tenant Conditional Access permits device login:
./scripts/local-test.sh --device-code
# Show endpoint, sign-in and tool-call progress:
./scripts/local-test.sh --verbose
```

The helper loads the project, model, tenant and demo client from Terraform outputs,
syncs locked dependencies, disables sensitive-content tracing and starts the demo.
It does not apply Terraform or use the hosted OAuth broker or caller bindings.
`--verbose` can be combined with `--device-code` or passed directly to
`uv run workplace-demo`. Diagnostics show the Foundry project/model, Work IQ MCP
destination, authentication progress, tool names and turn completion, not tokens,
prompts, tool arguments or document contents. SDK HTTP debug logging stays disabled.
For a failed Work IQ tool, opt in separately with
`./scripts/local-test.sh --verbose --show-tool-errors`. This prints bounded, escaped
error text locally with credentials and URL query strings redacted; successful
results and structured payloads are not printed. Error text may still contain
sensitive request metadata, so review it before sharing logs. This option does not
enable detailed errors in model-visible tool results or the hosted agent.
For manual configuration instead:

```bash
cp .env.example .env
# Edit the project/model, or load the Terraform outputs as below.
set -a; source .env; set +a
export FOUNDRY_PROJECT_ENDPOINT=$(terraform -chdir=terraform output -raw foundry_project_endpoint)
export AZURE_AI_MODEL_DEPLOYMENT_NAME=$(terraform -chdir=terraform output -raw chat_model_deployment)
export AZURE_TENANT_ID=$(terraform -chdir=terraform output -raw tenant_id)
export WORKIQ_CLIENT_ID=$(terraform -chdir=terraform output -raw workiq_demo_client_id)
uv run workplace-demo
# On a browserless machine, if tenant Conditional Access permits device login:
# uv run workplace-demo --device-code
```

Azure CLI must be signed in to an identity with project/model access. The demo
separately prompts for the Work IQ user's sign-in. Ask the document-discovery
question above, then a follow-up: the same in-memory history is used.
`/new` starts a new conversation; `/quit` or EOF closes clients and exits.
When a skill or tool requires approval, the demo shows the tool name and requested
arguments, then prompts `[y/N]`. Only `y` or `yes` approves; Enter denies.
It submits that decision and resumes the same turn before accepting another message.
Tokens and history are not saved to disk. This is a terminal demo, not a Foundry
Responses request or a simulation of platform caller headers.

## 4. Deploy and authorize the hosted agent

```bash
./scripts/deploy.sh
```

The script applies the platform while preserving any prior release, builds and
pushes a Linux/amd64 image, resolves its immutable ACR digest, and atomically saves
`agent_image_uri` in git-ignored `terraform/agent.auto.tfvars`. A second apply
registers the agent, deploys the same image as the OAuth broker, and assigns
instance-identity permissions. Both applies require confirmation.
It waits for the exact agent version/image and broker readiness.
Readiness proves neither user consent nor Microsoft 365 access.

Foundry injects `FOUNDRY_PROJECT_ENDPOINT` into the hosted container.
Do not declare `FOUNDRY_*` or `AGENT_*` variables in the agent's environment map;
these prefixes are reserved for the platform.

For a manual release, set `agent_image_uri` to
`YOUR_REGISTRY.azurecr.io/workplace-agent@sha256:YOUR_64_HEX_DIGEST` in
`terraform/agent.auto.tfvars`, review a plan, then apply.
Keep the persisted image input for later infrastructure changes; blanking it
plans agent and broker-container deletion. Keep previous images for rollback.
Only one deployment owner should manage the agent's latest version; pin production
clients to the version you have validated.

Before using the hosted agent, bind and authorize each intended user:

1. Temporarily set `enable_caller_diagnostics = true` and apply. Have the intended
   user invoke the agent normally through Foundry. An unbound invocation fails,
   but its trusted caller ID appears as `Foundry caller binding ID` in restricted
   hosted-agent logs. Never forge headers or accept an ID submitted by the browser.
2. An administrator verifies that invocation's owner and same-tenant Entra user
   object ID, sets `foundry_user_bindings = { "OPAQUE_CALLER_ID" = "ENTRA_USER_OBJECT_ID" }`,
   disables diagnostics again, and applies. Protect these identity-bearing logs
   and Terraform inputs. A service using delegated Foundry identities must enforce
   its own authenticated end-user boundary; project access alone is not that proof.
3. The bound user opens the broker URL printed by deployment with `/login`,
   signs in as that mapped Entra user, and completes authorization. The browser
   receives only a completion message, never a token. Retry in the Foundry Playground.

Conditional Access, revoked consent or expired authorization can require `/login`
again. Unknown callers never inherit another user's cached credentials.
If the wrapping key is replaced, existing caches reject the changed key:
users must authorize again. Retain required old key versions until cached
authorizations have been discarded; this sample does not migrate encrypted caches.

Check both a OneDrive PDF and an email PDF attachment against actual viewer pages.
Repeat as another user with different document permissions; inaccessible content
must not be disclosed. Also try scanned and non-PDF attachments and check explicit
limitations. Validate content guardrails separately from deployment readiness.
Do not invoke the container with forged platform headers.

## Development and cleanup

```bash
uv run pytest -q
uv run ruff check .
uv run pyright
terraform fmt -check -recursive terraform
terraform -chdir=terraform validate
bash -n scripts/deploy.sh
```

Offline tests cover synthetic multi-page PDFs, metadata/blob boundaries, direct
MCP calls, resource cleanup, local conversation reuse, scoped Cosmos history,
OAuth callback correlation/replay, application JWT validation, encrypted cache
isolation and concurrent-refresh leases. They do not read real Microsoft 365 data.
Live OAuth, Azure role propagation and tenant policy must still be checked.

For cleanup, the Work IQ service principal is shared tenant-wide and guarded
against destruction. Detach its state record (the Entra object is intentionally
left in place), then review and destroy the remaining stack:

```bash
terraform -chdir=terraform state rm azuread_service_principal.workiq
terraform -chdir=terraform plan -destroy
terraform -chdir=terraform destroy
```

This leaves the Work IQ service principal available to other tenant applications
and removes the hosted agent, broker, dedicated client applications, Cosmos data,
registry and telemetry. To reuse the stack later, import the retained service
principal again before applying. Key Vault purge protection prevents immediate
permanent removal; its soft-deleted vault/key remains protected during the
retention period. Do not purge a shared vault or weaken protection to speed
cleanup. Secure or remove local state and plan files according to organizational
policy.

## References

- [Enable Work IQ in your tenant](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/enable-work-iq)
- [Microsoft Work IQ admin setup sample](https://github.com/microsoft/work-iq-samples/blob/main/ADMIN_SETUP.md)
- [Work IQ MCP overview](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/overview)
- [Work IQ tools and binary retrieval](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/tool-reference)
- [Managed identity as an application credential](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-config-app-trust-managed-identity)
- [Hosted-agent permissions and identity delegation](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-permissions)
- [Terraform hosted-agent deployment contract](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-terraform)
