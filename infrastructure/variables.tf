
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
  description = "Max vCPUs of the GPU Batch compute environment. Caps the GPU spend rate: 16 = one g6.4xlarge or g5.4xlarge at a time (at most $2.06/h). batch_gpu.tf refuses a cap above the smallest gpu_instance_types' vCPUs, so it is always one instance at a time."
  type        = number
  default     = 16
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
    state machine (a change is an in-place state machine update; the job definitions don't change).
    Must be a valid Fargate combination (0.25 vCPU: 512, 1024 or 2048 MiB; 0.5: 1-4 GiB; 1: 2-8 GiB;
    2: 4-16 GiB; 4: 8-30 GiB; checked below). QC and align run at the same time, and the region
    planner runs after QC alongside align, so qc + align and plan + align must fit in fargate_max_vcpus.
    Sized from the real-sample runs (REAL-HG003-35x/-30x chr20, 2-3 Nov 2026; #173): peak memory
    align 2.9 GiB, qc 1.1 GiB, a variants shard 126 MiB, the merge 81 MiB; CPU in use align ~3.9 vCPU,
    qc 0.6-1.0, a shard ~0.95 (bcftools is single-threaded), the merge ~0.1.
  EOT
  type        = map(object({ vcpu = number, memory_mb = number }))
  default = {
    qc       = { vcpu = 1, memory_mb = 2048 }   # fastp: peak 1.1 GiB; already the 1-vCPU minimum
    align    = { vcpu = 4, memory_mb = 8192 }   # bwa-mem2, sort, markdup: CPU-bound (3.9 of 4 vCPU), peak 2.9 GiB
    variants = { vcpu = 1, memory_mb = 2048 }   # one shard: bcftools mpileup | call, single-threaded, peak 126 MiB
    plan     = { vcpu = 0.25, memory_mb = 512 } # region planner: reads the reference index, ~24 s
    merge    = { vcpu = 0.25, memory_mb = 512 } # concat the parts, index, stats: peak 81 MiB, ~2 s of CPU
  }

  validation {
    condition     = toset(keys(var.stage_resources)) == toset(["qc", "align", "variants", "plan", "merge"])
    error_message = "stage_resources needs exactly the stages qc, align, variants (a shard), plan and merge."
  }
  validation {
    condition = alltrue([for r in values(var.stage_resources) : contains(keys(local.fargate_memory_mb), tostring(r.vcpu)) && (
      r.memory_mb >= local.fargate_memory_mb[tostring(r.vcpu)][0] &&
      r.memory_mb <= local.fargate_memory_mb[tostring(r.vcpu)][1] &&
      (r.memory_mb % 1024 == 0 || (r.vcpu == 0.25 && r.memory_mb == 512))
    )])
    error_message = "Each stage needs a valid Fargate vCPU/memory pair: 0.25 vCPU 512, 1024 or 2048 MiB; 0.5 vCPU 1024-4096; 1 vCPU 2048-8192; 2 vCPU 4096-16384; 4 vCPU 8192-30720 (1024 MiB steps)."
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
  description = "Time limit for a whole pipeline run, queue waits included. A whole genome on the large route (v1.3) is ~4-4.5 h (pre-flight 2 h, chunks, merge 4 h limits), and v1.5's split DeepVariant adds ~3.7 h, so 24 h leaves room for retries and queueing. A run past it ends as TimedOut (no SNS message; it shows on the dashboard)."
  type        = number
  default     = 24

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

variable "alignment_expiry_days" {
  description = "Days before alignments/ (CRAM + index) expire (storage_lifecycle.tf): the break-even of storing a CRAM vs re-aligning its raw reads, 178-248 days on the six real chr20 runs (scripts/lifecycle_break_even.py). 0 turns the rule off (alignments kept forever)."
  type        = number
  default     = 180

  validation {
    condition     = var.alignment_expiry_days == 0 || var.alignment_expiry_days >= 30
    error_message = "alignment_expiry_days must be 0 (never expire) or at least 30: a CRAM must outlive any rerun of the variant calling that needs it."
  }
}

# --- Cloud DeepVariant (G2: #43, #44; docs/design/cloud-deepvariant.md) ---

variable "gpu_instance_types" {
  description = "Instance types the GPU Batch environment may launch, each in batch_gpu.tf's gpu_instance_shapes. Both have a 24 GB-class GPU (call_variants at batch 1024 needs more than 10 GB). g6 (L4) is about 20% cheaper than g5 (A10G) in eu-west-2. The 4xlarge (16 vCPU / 64 GiB) types are the measured shape (docs/design/cloud-deepvariant.md): cheaper per run than the xlarge, since make_examples runs four times faster."
  type        = list(string)
  default     = ["g6.4xlarge", "g5.4xlarge"]
}

variable "deepvariant_resources" {
  description = "vCPU and memory (MiB) per DeepVariant job, plus one GPU. The measured shape: 16 vCPU (~8 min for chr20). It must fit gpu_max_vcpus and the smallest gpu_instance_types (deepvariant.tf)."
  type        = object({ vcpu = number, memory_mb = number })
  default     = { vcpu = 16, memory_mb = 57344 } # 64 GiB instance; 56 GiB leaves room for the ECS agent and OS (need: 0.6 GB per shard + 2 GB)
}

variable "deepvariant_image_version" {
  description = "Tag of the geyser-deepvariant image in ECR (pushed by hand: ~9.8 GB)."
  type        = string
  default     = "dev-local"
}

# --- vg giraffe as the aligner (#146, giraffe.tf) ---

variable "giraffe_image_version" {
  description = "Tag of the geyser-giraffe image (app-giraffe/Dockerfile, built FROM a geyser-app image) in ECR. Build it from the same commit as geyser-app's image_version, so the two run the same tasks.py."
  type        = string
  default     = "dev-local"
}

variable "giraffe_graph" {
  description = "The pangenome graph giraffe maps to: a .gbz under pangenome/ in the data lake, with its .hapl beside it (GIRAFFE_GRAPH). Empty: the app's default, <reference stem>.gbz. The default is HPRC v1.1's GRCh38 graph cut to chr20, the file the local benchmarks (#129, #191) and the pangenome-aware DeepVariant caller use; the stage refuses a graph whose GRCh38 paths don't match the run's reference."
  type        = string
  default     = "hprc-v1.1-mc-grch38.chr20.gbz"

  validation {
    condition     = var.giraffe_graph == "" || (endswith(var.giraffe_graph, ".gbz") && !strcontains(var.giraffe_graph, "/"))
    error_message = "giraffe_graph must be empty or a .gbz file name (no directory: it is read from pangenome/)."
  }
}

variable "giraffe_kmc_memory_gb" {
  description = "kmc's memory cap in GB for haplotype sampling (KMC_MEMORY_GB). 4 is the app default; plus ~1 GiB it must fit in the align stage's memory. Larger only saves kmc temp disk: HG003 chr20 at -m8 took 37 s and 7.4 GiB."
  type        = number
  default     = 4

  validation {
    condition     = var.giraffe_kmc_memory_gb >= 1 && floor(var.giraffe_kmc_memory_gb) == var.giraffe_kmc_memory_gb
    error_message = "giraffe_kmc_memory_gb must be a whole number of GB, at least 1."
  }
}

variable "gpu_disk_gb" {
  description = "Root volume (gp3) of the GPU instances. The DeepVariant image unpacks to ~28 GB, plus the alignment (~17 GB CRAM for a 30x genome) and DeepVariant's intermediates (or, in v1.5 split mode, every part's examples): 100 GB is enough for a chromosome, a whole genome needs 250+ (500 leaves room for a 30x genome's examples). 500 GB of gp3 is ~$0.06 per instance-hour, billed only while an instance runs."
  type        = number
  default     = 500

  validation {
    condition     = var.gpu_disk_gb >= 100 && var.gpu_disk_gb <= 1000 && floor(var.gpu_disk_gb) == var.gpu_disk_gb
    error_message = "gpu_disk_gb must be a whole number of GB from 100 (the image alone is ~28 GB) to 1000."
  }
}

variable "default_caller" {
  description = "Variant caller for runs the trigger starts: bcftools (Fargate) or deepvariant (GPU, falls back to bcftools if it fails). Keep bcftools until the GPU environment is applied and a cloud run has passed."
  type        = string
  default     = "bcftools"

  validation {
    condition     = contains(["bcftools", "deepvariant"], var.default_caller)
    error_message = "default_caller must be bcftools or deepvariant."
  }
}

variable "gate_mode" {
  description = "What a failed quality-gate verdict does to a cloud run (#144). warn: recorded (qc_reports/{id}.gate.json, the run record, the execution history) and the run succeeds. fail: the run fails through Notify_Failure with the gate's reasons. fail since #157 (the user's decision, 3 Nov 2026: real-sample runs passed with no false fails, and the cloud smoke tests pass with gate_profile smoke)."
  type        = string
  default     = "fail"

  validation {
    condition     = contains(["warn", "fail"], var.gate_mode)
    error_message = "gate_mode must be warn or fail."
  }
}

# --- v1.3 Big inputs: the large route (genome_scale.tf, the state machine's Large_QC_and_Align) ------------

variable "large_input_gb" {
  description = "Uploads at or above this size (GB, 10^9 bytes, both mates added up) take the large route: pre-flight, chunked alignment, a streaming merge. Smaller ones take today's path, unchanged. A manual start can pass input_bytes to choose (e.g. a chr20 canary on the large route)."
  type        = number
  default     = 5

  validation {
    condition     = var.large_input_gb > 0
    error_message = "large_input_gb must be above 0."
  }
}

variable "large_route_resources" {
  description = <<-EOT
    vCPU and memory (MiB) of the large route's jobs (docs/design/genome-scale-app.md, the plan's sizes). All run on
    the large-disk job definition. QC runs alongside the pre-flight and then alongside the chunks, so
    qc + align_concurrency x chunk must fit in fargate_spot_max_vcpus (step_functions.tf derives the maximum).
    Fargate pairs: 4 vCPU 8-30 GiB (1 GiB steps), 8 vCPU 16-60 GiB (4 GiB steps), 16 vCPU 32-120 GiB (8 GiB steps).
  EOT
  type        = map(object({ vcpu = number, memory_mb = number }))
  default = {
    preflight = { vcpu = 8, memory_mb = 16384 } # one streaming pass: gunzip, count, cut, pigz -1
    qc        = { vcpu = 8, memory_mb = 16384 } # fastp on the whole input (os.cpu_count() threads)
    chunk     = { vcpu = 8, memory_mb = 32768 } # bwa-mem2 with the GRCh38 index in memory (~16-20 GB: measure on the canary)
    merge     = { vcpu = 8, memory_mb = 16384 } # samtools merge (streaming) | markdup | CRAM, mosdepth
  }

  validation {
    condition     = toset(keys(var.large_route_resources)) == toset(["preflight", "qc", "chunk", "merge"])
    error_message = "large_route_resources needs exactly preflight, qc, chunk and merge."
  }
  validation {
    condition = alltrue([for r in values(var.large_route_resources) : (
      (r.vcpu == 4 && r.memory_mb >= 8192 && r.memory_mb <= 30720 && r.memory_mb % 1024 == 0) ||
      (r.vcpu == 8 && r.memory_mb >= 16384 && r.memory_mb <= 61440 && r.memory_mb % 4096 == 0) ||
      (r.vcpu == 16 && r.memory_mb >= 32768 && r.memory_mb <= 122880 && r.memory_mb % 8192 == 0)
    )])
    error_message = "Each large-route job needs a valid Fargate pair: 4 vCPU 8192-30720 MiB (1024 steps), 8 vCPU 16384-61440 (4096 steps), 16 vCPU 32768-122880 (8192 steps)."
  }
}

variable "large_disk_gb" {
  description = "Ephemeral storage of the large-disk job definition (GiB, Fargate's 21-200, billed per GiB-hour above 20): the pre-flight and QC hold the whole input (~53 GB for a 30x genome), the merge its CRAM and statistics."
  type        = number
  default     = 200

  validation {
    condition     = var.large_disk_gb >= 21 && var.large_disk_gb <= 200
    error_message = "large_disk_gb must be 21-200 (the Fargate range)."
  }
}

variable "genome_chunk_count" {
  description = "Default chunk_count (a per-run setting, 2-96): how many equal chunks the pre-flight cuts a large input into."
  type        = number
  default     = 24

  validation {
    condition     = var.genome_chunk_count >= 2 && var.genome_chunk_count <= 96 && floor(var.genome_chunk_count) == var.genome_chunk_count
    error_message = "genome_chunk_count must be a whole number from 2 to 96."
  }
}

variable "genome_align_concurrency" {
  description = "Default align_concurrency (a per-run setting): chunks aligned at once. At most (fargate_spot_max_vcpus - qc) / chunk vCPUs, checked in step_functions.tf: (48 - 8) / 8 = 5."
  type        = number
  default     = 5

  validation {
    condition     = var.genome_align_concurrency >= 1 && floor(var.genome_align_concurrency) == var.genome_align_concurrency
    error_message = "genome_align_concurrency must be a whole number >= 1."
  }
}

variable "genome_merge_threads" {
  description = "Default merge_threads (a per-run setting): samtools threads in the merge, at most the merge job's vCPUs."
  type        = number
  default     = 8

  validation {
    condition     = var.genome_merge_threads >= 1 && floor(var.genome_merge_threads) == var.genome_merge_threads
    error_message = "genome_merge_threads must be a whole number >= 1."
  }
}

variable "raw_reads_tier_days" {
  description = "Days before a raw_reads/ object of at least large_input_gb (5 GB: the large route's threshold, the user's decision) moves to S3 Glacier Instant Retrieval (still readable at once, ~80% cheaper to store; a per-GB retrieval charge and a 90-day minimum apply). Smaller uploads stay in Standard. 30 keeps re-runs within a build window free of retrieval charges. 0 turns the rule off."
  type        = number
  default     = 30

  validation {
    condition     = var.raw_reads_tier_days == 0 || var.raw_reads_tier_days >= 1
    error_message = "raw_reads_tier_days must be 0 (off) or at least 1."
  }
}

variable "genome_chunk_expiry_days" {
  description = "Days before the large route's chunks (intermediate/chunks/, intermediate/aligned/: ~130 GB per 30x genome) expire. Nothing reads them after the merge; shorter than intermediate_expiry_days because of their size."
  type        = number
  default     = 3

  validation {
    condition     = var.genome_chunk_expiry_days >= 1
    error_message = "genome_chunk_expiry_days must be at least 1: the merge must still find the chunks."
  }
}
