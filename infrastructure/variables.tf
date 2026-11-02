
# infrastructure/variables.tf

variable "project_name" {
  description = "A unique name for the project to prefix resources."
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g., dev, prod)."
  type        = string
  default     = "dev"
}

variable "image_version" {
  description = "A unique identifier for the application version, e.g., a git commit SHA."
  type        = string
  default     = "latest" # A sensible default for local runs
}

variable "enable_nat_gateway" {
  description = "Create a NAT gateway (and its EIP) for the private subnet. Bills hourly, so off by default; Fargate jobs run in the public subnet instead."
  type        = bool
  default     = false
}

variable "monthly_budget_usd" {
  description = "Monthly AWS cost budget in USD. Alert emails go out at 50% and 100% of actual spend and at 100% of forecast spend."
  type        = number
  default     = 10
}

variable "budget_alert_email" {
  description = "Email address for AWS Budget alerts. Set it in terraform.tfvars; it is deliberately not committed."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_alert_email))
    error_message = "budget_alert_email must be an email address."
  }
}

# --- Claude Code guardrails (#73, claude_guardrails.tf) ---

variable "gpu_max_vcpus" {
  description = "Max vCPUs of the GPU Batch compute environment. Caps the GPU spend rate: 4 = one g5.xlarge (or g4dn.xlarge) at a time."
  type        = number
  default     = 4
}

variable "fargate_max_vcpus" {
  description = "Max vCPUs of the Fargate Batch compute environment. Caps the CPU spend rate: 8 = four 2-vCPU stage jobs at a time."
  type        = number
  default     = 8
}

variable "claude_role_trusted_user" {
  description = "IAM user allowed to assume the geyser-claude role (the user's own CLI identity)."
  type        = string
  default     = "harry-admin"
}

variable "claude_project_cap_usd" {
  description = "Total AWS spend allowed for the project from claude_project_cap_start (account-wide)."
  type        = number
  default     = 20
}

variable "claude_project_cap_start" {
  description = "Start of the project budget period, format YYYY-MM-DD_hh:mm (UTC)."
  type        = string
  default     = "2026-11-02_00:00"
}

variable "claude_kill_switch_usd" {
  description = "Actual spend (USD, since claude_project_cap_start) at which AWS Budgets attaches the deny-all kill switch to geyser-claude. Below the cap to leave room for the 8-24 h billing lag."
  type        = number
  default     = 8

  validation {
    condition     = var.claude_kill_switch_usd > 0 && var.claude_kill_switch_usd < var.claude_project_cap_usd
    error_message = "claude_kill_switch_usd must be above 0 and below claude_project_cap_usd."
  }
}
