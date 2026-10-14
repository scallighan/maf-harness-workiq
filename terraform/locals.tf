locals {
  func_name      = "wiq${random_string.unique.result}"
  loc_for_naming = lower(replace(var.location, " ", ""))
  gh_repo        = split("/", var.gh_repo)[1]

  foundry_name        = "aif${local.func_name}-managed"
  foundry_project     = "fp${local.func_name}"
  foundry_endpoint    = "https://${local.foundry_name}.services.ai.azure.com/api/projects/${local.foundry_project}"
  toolbox_endpoint    = "${local.foundry_endpoint}/toolboxes/${var.workiq_toolbox_name}/mcp?api-version=v1"
  workiq_mcp_endpoint = "https://workiq.svc.cloud.microsoft/mcp"

  deploy_agent = var.agent_image_uri != ""

  workiq_resource_app_id = "fdcc1f02-fc51-4226-8753-f668596af7f7"
  workiq_ask_scope_id    = "0b1715fd-f4bf-4c63-b16d-5be31f9847c2"

  foundry_operation_retry = {
    error_message_regex  = ["(?i)Another operation is in progress"]
    interval_seconds     = 5
    max_interval_seconds = 30
  }

  tags = {
    managed_by = "terraform"
    repo       = local.gh_repo
    workload   = "workplace-productivity-agent"
  }
}
