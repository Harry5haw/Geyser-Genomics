# infrastructure/account/variables.tf
# Read from the main stack's terraform.tfvars (-var-file=../terraform.tfvars); Terraform only warns about
# the variables there that this stack doesn't declare.

variable "project_name" {
  description = "Project name prefix, as in the main stack."
  type        = string
  default     = "geyser"
}

variable "environment" {
  description = "Environment tag, as in the main stack."
  type        = string
  default     = "dev"
}

variable "budget_alert_email" {
  description = "Address for the Cost Anomaly Detection emails (the same as the budget alerts). Set in terraform.tfvars; never committed."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_alert_email))
    error_message = "budget_alert_email must be an email address."
  }
}

variable "anomaly_alert_usd" {
  description = "Email only for cost anomalies whose total impact is at least this many US dollars."
  type        = number
  default     = 1
}
