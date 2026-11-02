
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

variable "fargate_spot_max_vcpus" {
  description = "Max vCPUs of the Fargate Spot compute environment (the queue's first choice). Its own cap, on top of fargate_max_vcpus: at both caps 16 vCPUs can run, the Spot half at Spot prices."
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

# --- Resilience and per-stage sizing (#104) ---

variable "stage_resources" {
  description = <<-EOT
    vCPU and memory (MiB) per pipeline stage, sent as ContainerOverrides.ResourceRequirements by the
    state machine. Must be a valid Fargate combination (1 vCPU: 2-8 GiB, 2: 4-16 GiB, 4: 8-30 GiB, in
    1 GiB steps; checked below). QC and align run at the same time, so qc + align vCPU must fit in
    fargate_max_vcpus.
  EOT
  type        = map(object({ vcpu = number, memory_mb = number }))
  default = {
    qc       = { vcpu = 1, memory_mb = 2048 } # fastp: light
    align    = { vcpu = 4, memory_mb = 8192 } # bwa-mem2 (+ its index build), sort, markdup
    variants = { vcpu = 2, memory_mb = 4096 } # bcftools mpileup | call: single-threaded, small
  }

  validation {
    condition     = toset(keys(var.stage_resources)) == toset(["qc", "align", "variants"])
    error_message = "stage_resources needs exactly the stages qc, align and variants."
  }
  validation {
    condition = alltrue([for r in values(var.stage_resources) : contains(keys(local.fargate_memory_mb), tostring(r.vcpu)) && (
      r.memory_mb >= local.fargate_memory_mb[tostring(r.vcpu)][0] &&
      r.memory_mb <= local.fargate_memory_mb[tostring(r.vcpu)][1] &&
      r.memory_mb % 1024 == 0
    )])
    error_message = "Each stage needs a valid Fargate vCPU/memory pair: 1 vCPU 2048-8192 MiB, 2 vCPU 4096-16384, 4 vCPU 8192-30720, in 1024 MiB steps."
  }
}

variable "job_disk_gb" {
  description = "Fargate ephemeral storage per job, in GiB. 20 (the default) is free and needs no setting; 21-200 is billed per GiB-hour. A real 30x sample needs ~100+."
  type        = number
  default     = 20

  validation {
    condition     = var.job_disk_gb == 20 || (var.job_disk_gb >= 21 && var.job_disk_gb <= 200)
    error_message = "job_disk_gb must be 20 (the free default) or 21-200 (the Fargate range)."
  }
}

variable "pipeline_timeout_hours" {
  description = "Time limit for a whole pipeline run, queue waits included. The longest path is align (4 h attempt limit) then variants (2 h), so 12 h leaves room for retries and queueing. A run past it ends as TimedOut (no SNS message; it shows on the dashboard)."
  type        = number
  default     = 12

  validation {
    condition     = var.pipeline_timeout_hours >= 1 && var.pipeline_timeout_hours <= 48
    error_message = "pipeline_timeout_hours must be between 1 and 48."
  }
}

variable "log_retention_days" {
  description = "Days to keep Batch job logs and trigger Lambda logs (the Step Functions error log keeps 7)."
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be a value CloudWatch Logs accepts (1, 3, 5, 7, 14, 30, 60, 90, ...)."
  }
}

variable "variant_region_window" {
  description = "Scatter-gather variant calling (#108): bases per region. The planner (tasks.py regions) and the merge (variants --window) both get this value. 50 Mb = 2 shards for GRCh38 chr20."
  type        = number
  default     = 50000000

  validation {
    condition     = var.variant_region_window >= 1000 && floor(var.variant_region_window) == var.variant_region_window
    error_message = "variant_region_window must be a whole number of bases, at least 1000."
  }
}

variable "variants_max_concurrency" {
  description = "Most variant-calling shards (Map iterations) running at once. 4 x the variants size (2 vCPU) = the 8-vCPU Fargate cap."
  type        = number
  default     = 4

  validation {
    condition     = var.variants_max_concurrency >= 1 && floor(var.variants_max_concurrency) == var.variants_max_concurrency
    error_message = "variants_max_concurrency must be a whole number >= 1."
  }
}

variable "intermediate_expiry_days" {
  description = "Days before the data lake's disposable intermediates (intermediate/, decompressed/) expire and unfinished multipart uploads are aborted (storage_lifecycle.tf). Outputs and raw reads never expire."
  type        = number
  default     = 7

  validation {
    condition     = var.intermediate_expiry_days >= 1
    error_message = "intermediate_expiry_days must be at least 1: a merge must still find its parts."
  }
}
