variable "subscription_id" {
  type      = string
  sensitive = true
}

variable "location" {
  type    = string
  default = "EastUS2"
}

variable "gh_repo" {
  type        = string
  description = "GitHub repository in owner/name form used for naming."
  default     = "scallighan/maf-harness-workiq"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.gh_repo))
    error_message = "Use a GitHub repository in owner/name form."
  }
}

variable "chat_model" {
  type        = string
  description = "Azure AI Foundry chat model to deploy for the agent."
  default     = "gpt-5.6-luna"
}

variable "chat_model_version" {
  type    = string
  default = "2026-07-09"
}

variable "chat_model_capacity" {
  type        = number
  description = "TPM capacity (in thousands) for the chat model deployment."
  default     = 50
}

variable "user_object_ids" {
  type        = set(string)
  description = "Entra object IDs granted Foundry User on the project, not Microsoft 365 data access."
  default     = []
}

variable "workiq_toolbox_name" {
  type        = string
  description = "Foundry Toolbox exposing the custom Work IQ MCP connection."
  default     = "work-iq-toolbox"

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]+$", var.workiq_toolbox_name))
    error_message = "Use a toolbox name containing only letters, digits, underscores and hyphens."
  }
}

variable "workiq_mcp_connection_name" {
  type        = string
  description = "Terraform-managed custom OAuth MCP connection; import an existing connection before applying."
  default     = "work-iq-mcp"

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]+$", var.workiq_mcp_connection_name))
    error_message = "Use a connection name containing only letters, digits, underscores and hyphens."
  }
}

variable "workiq_mcp_client_secret" {
  type        = string
  description = "Existing OAuth app secret for connection creation/rotation only; omitted when importing/reusing stored credentials."
  sensitive   = true
  ephemeral   = true
  default     = null
}

variable "workiq_mcp_secret_version" {
  type        = string
  description = "Increment when supplying a new ephemeral secret to rotate the connection credential."
  default     = "0"
}

variable "workiq_mcp_allowed_tools" {
  type        = list(string)
  description = "Exact upstream MCP tool names exposed to the agent; defaults exclude mutation tools."
  default     = ["ask", "fetch", "get_schema", "search_paths", "fetch_blob", "retrieve"]

  validation {
    condition = (
      length(var.workiq_mcp_allowed_tools) > 0 &&
      length(distinct(var.workiq_mcp_allowed_tools)) == length(var.workiq_mcp_allowed_tools) &&
      alltrue([for name in var.workiq_mcp_allowed_tools : can(regex("^[A-Za-z0-9_-]+$", name))])
    )
    error_message = "Provide a nonempty list of unique upstream MCP tool names."
  }
}

variable "workiq_mcp_require_approval" {
  type        = string
  description = "Whether the agent must obtain confirmation before calling the allowed MCP tools."
  default     = "never"

  validation {
    condition     = contains(["always", "never"], var.workiq_mcp_require_approval)
    error_message = "Use always or never for MCP tool approval."
  }
}

variable "ui_image_uri" {
  type        = string
  description = "Immutable UI image URI; blank leaves the optional Azure chat UI unprovisioned."
  default     = ""

  validation {
    condition     = var.ui_image_uri == "" || can(regex("^[a-z0-9.-]+\\.azurecr\\.io/[a-z0-9/._-]+@sha256:[0-9a-f]{64}$", var.ui_image_uri))
    error_message = "Use an immutable ACR image URI ending in @sha256:<64 hex characters>."
  }
}

variable "ui_min_replicas" {
  type        = number
  description = "Minimum Container Apps UI replicas; zero scales to zero when idle."
  default     = 0

  validation {
    condition     = var.ui_min_replicas >= 0 && var.ui_min_replicas <= 2 && floor(var.ui_min_replicas) == var.ui_min_replicas
    error_message = "Use an integer from zero to two."
  }
}

variable "ui_additional_redirect_uris" {
  type        = list(string)
  description = "Optional additional SPA redirects, for example http://localhost:5173/ for local frontend development."
  default     = []

  validation {
    condition = alltrue([
      for uri in var.ui_additional_redirect_uris :
      can(regex("^https://[^/?#]+/[^?#]*$", uri)) || can(regex("^http://localhost:[0-9]+/[^?#]*$", uri))
    ])
    error_message = "Use HTTPS redirects, or localhost HTTP redirects with an explicit port."
  }
}

variable "workiq_toolbox_redirect_uri" {
  type        = string
  description = "Foundry OAuth redirect URL; scripts/setup_toolbox.py persists this nonsecret value."
  default     = ""

  validation {
    condition     = var.workiq_toolbox_redirect_uri == "" || can(regex("^https://[^/?#]+/", var.workiq_toolbox_redirect_uri))
    error_message = "Use the HTTPS OAuth redirect URL returned by the Foundry connection."
  }
}

# scripts/deploy.sh persists release inputs in agent.auto.tfvars.
variable "agent_name" {
  type        = string
  description = "Name of the Foundry hosted agent."
  default     = "workplace-productivity"
}

variable "agent_image_uri" {
  type        = string
  description = "Immutable ACR image URI. Leave blank for a platform-only apply."
  default     = ""

  validation {
    condition     = var.agent_image_uri == "" || can(regex("^[a-z0-9.-]+\\.azurecr\\.io/[a-z0-9/._-]+@sha256:[0-9a-f]{64}$", var.agent_image_uri))
    error_message = "Use an immutable ACR image URI ending in @sha256:<64 hex characters>."
  }

}

variable "history_retention_days" {
  type        = number
  description = "Cosmos message TTL; deletion is asynchronous. Foundry Responses retention is separate."
  default     = 30

  validation {
    condition     = var.history_retention_days >= 1 && floor(var.history_retention_days) == var.history_retention_days
    error_message = "History retention must be a positive whole number of days."
  }
}

variable "agent_cpu" {
  type    = string
  default = "1"
}

variable "agent_memory" {
  type    = string
  default = "2Gi"

  validation {
    condition     = contains(["0.5:1Gi", "1:2Gi", "2:4Gi"], "${var.agent_cpu}:${var.agent_memory}")
    error_message = "Choose a supported CPU/memory pair: 0.5/1Gi, 1/2Gi, or 2/4Gi."
  }
}
