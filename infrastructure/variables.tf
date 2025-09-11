
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