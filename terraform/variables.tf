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
  description = "Foundry Toolbox containing the documented Work IQ built-in tool."
  default     = "work-iq-toolbox"

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]+$", var.workiq_toolbox_name))
    error_message = "Use a toolbox name containing only letters, digits, underscores and hyphens."
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
