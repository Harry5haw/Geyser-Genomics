
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