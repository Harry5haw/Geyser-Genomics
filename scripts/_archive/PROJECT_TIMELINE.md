## 2025-06-16
- Initial commit (commit: b20c715)

## 2025-06-16
- Initial commit: Port foundational DAGs and Dockerfile from local simulation (commit: 6f0b29f)

## 2025-06-16
- feat(terraform): Define S3 bucket and data structure (commit: 24dbd3c)

## 2025-06-17
- feat(terraform): Define S3 data lake and ECR repository (commit: 9b7cc14)

## 2025-06-17
- feat(dag): Refactor v6 decompression task to be cloud-native (commit: 4aea60b)

## 2025-06-17
- docs: Add CHANGELOG and document major architectural pivot (commit: ee1f67e)

## 2025-06-17
- feat(terraform): Define AWS Batch compute environment and job queue (commit: b936850)

## 2025-06-17
- feat(app): Create tasks.py CLI and implement streaming decompress task (commit: 618f44f)

## 2025-06-17
- feat(app): Implement all bioinformatics tasks in tasks.py (commit: 2c039df)

## 2025-06-17
- build: Finalize Dockerfile with all tools and app code (commit: 2284648)

## 2025-06-17
- feat(infra): Define complete AWS infrastructure with Terraform (commit: de7818d)

## 2025-06-17
- feat(dag): Add final pipeline DAG for AWS Batch orchestration (commit: 1a2a4c9)

## 2025-09-03
- docs folder (commit: 2c086aa)

## 2025-09-03
- images for readme. (commit: 2d11bde)

## 2025-09-03
- Adding Images (commit: cc302a6)

## 2025-09-03
- family samples 2 image (commit: 8009dd1)

## 2025-09-03
- Add files via upload (commit: e16a410)

## 2025-09-03
- Add files via upload (commit: 2ab6d98)

## 2025-09-03
- Add files via upload (commit: f1ea321)

## 2025-09-03
- Add files via upload (commit: 7c6358f)

## 2025-09-03
- Mermaid fix (commit: 0ac2337)

## 2025-09-03
- remove unicode emojis.md (commit: af0cc6b)

## 2025-09-04
- Clarity on what it does.md (commit: 47d1755)

## 2025-09-04
- contents plus developer section (commit: 4e56156)

## 2025-09-04
- Deployment guide added (commit: fd2dbee)

## 2025-09-04
- content deployment guide fix (commit: 280ea52)

## 2025-09-04
- Technology Banner (commit: 1bdabce)

## 2025-09-04
- Add files via upload (commit: 63e9fff)

## 2025-09-04
- Add files via upload (commit: db31438)

## 2025-09-04
- Add files via upload (commit: f717bb1)

## 2025-09-04
- Add files via upload (commit: cc095a3)

## 2025-09-04
- Add files via upload (commit: d29a306)

## 2025-09-05
- Geyser Rebrand.md (commit: 933814c)

## 2025-09-05
- Add files via upload (commit: 9eb86dd)

## 2025-09-05
- Add files via upload (commit: fc6d48d)

## 2025-09-11
- feat(infra): Establish new VPC network foundation with NAT Gateway (commit: 7db1b32)

## 2025-09-11
- feat(infra): Deploy AWS Batch platform, storage, and IAM roles (commit: 4b22386)

## 2025-09-11
- feat(app): Containerize bioinformatics application for AWS Batch (commit: 2bc6dc9)

## 2025-09-11
- refactor(app): Decouple S3 bucket and fix stream/dependency bugs (commit: 1066e05)

## 2025-09-11
- feat(airflow): Integrate DAG with BatchOperator and configure credentials (commit: 20f65d1)

## 2025-09-11
- chore: Add comprehensive gitignore for project files (commit: 0e71dba)

## 2025-09-11
- feat(airflow): Parameterize DAG for dynamic sample processing (commit: 28b2f0d)

## 2025-09-11
- fix(app): Add sra-toolkit and fix NCBI interactive config issue (commit: 0e24d61)

## 2025-09-11
- feat(pipeline): Parameterize DAG and tasks for dynamic sample/reference (commit: a1b4a50)

## 2025-09-11
- chore(infra): Update job definition to use v1.3 application image (commit: f22a552)

## 2025-09-11
- feat: Add GitHub Actions CI/CD for ECR image push (commit: 74f7322)

## 2025-09-11
- fix: Dockerfile COPY paths for CI/CD context (commit: d45f96b)

## 2025-09-11
- feat(orchestration): Complete migration to AWS Step Functions (#1) (commit: a3e31f6)

## 2025-09-11
- feat(terraform): Configure S3 remote state backend (commit: da6b3a4)

## 2025-09-11
- feat(cicd): Add GitHub Actions workflow for Terraform (commit: 096875c)

## 2025-09-11
- chore: Add debug step to infra workflow (commit: 57976fb)

## 2025-09-11
- chore: Debug OIDC by testing with ECR role (commit: 58af058)

## 2025-09-11
- chore: Revert OIDC debug and point to recreated Terraform role (commit: b755023)

## 2025-09-11
- chore: Hijack ECR workflow for definitive OIDC test (commit: f9b42dc)

## 2025-09-11
- chore: Revert ECR workflow (commit: 1004abf)

## 2025-09-11
- fix(cicd): Create clean Terraform workflow (commit: 4c0a4dd)

## 2025-09-12
- style: Add comments and finalize CI/CD workflow configurations (commit: e66dd58)

## 2025-09-12
- feat(debug): Add OIDC token dump workflow (commit: 47d6220)

## 2025-09-12
- fix(debug): Correct the OIDC token dump workflow (commit: 92153cf)

## 2025-09-12
- fix(debug): Use manual curl to dump OIDC token payload (commit: 9628d8e)

## 2025-09-12
- fix(cicd): Resolve OIDC auth with new TerraformDeployerRole (commit: f49b2fd)

## 2025-09-12
- fix(cicd): Remove pull-requests permission to isolate OIDC issue (commit: ee166d3)

## 2025-09-12
- fix(cicd): Finalize Terraform workflow and resolve OIDC permissions issue (commit: 8496cd6)

## 2025-09-12
- chore(infra): Add comment to trigger CI/CD workflow (commit: 3de5358)

## 2025-09-12
- chore(infra): Final trigger for CI/CD validation (commit: 6a2b5b7)

## 2025-09-12
- fix(cicd): Final reset of infra workflow to known-good state (commit: f59528f)

## 2025-09-12
- feat(terraform): Manage CI/CD IAM roles and cleanup manual resources (commit: 8231af2)

## 2025-09-12
- feat(terraform): Finalize management of CI/CD IAM roles (commit: 712c368)

## 2025-09-12
- chore: Reset infra workflow to stable baseline (commit: 1cf79fd)

## 2025-09-12
- feat(cicd): Implement final PR-based workflow for Terraform (commit: 88334d2)

## 2025-09-12
- fix(cicd): Finalize and correct the PR-based infra workflow (commit: 7639d6e)

## 2025-09-12
- feat: Implement S3 event-driven trigger for pipeline (commit: f6713d8)

## 2025-09-12
- feat: Instrument application with CloudWatch custom metrics (commit: 261f9c2)

## 2025-09-12
- Add files via upload (commit: b0b63ba)

## 2025-09-13
- feat: Enhance observability & force Batch job definition updates (commit: a5f35aa)

## 2025-09-13
- fix(cicd): Standardize and correct all CI/CD workflow configurations (commit: 2c7084f)

## 2025-09-13
- chore: Trigger infrastructure workflow with final config (commit: cca2d05)

## 2025-09-13
- style: Format Terraform code according to official standard (commit: 24d2fb8)

## 2025-09-13
- chore: Trigger application build to deploy metrics instrumentation (commit: 0ff332b)

## 2025-09-13
- fix(cicd): Add --no-cache to app build to ensure freshness (commit: 6aed7e5)

## 2025-09-13
- chore: Trigger final application build (commit: ec8741e)

## 2025-09-13
- fix(app): Force update of tasks.py with correct instrumentation (commit: c0cd172)

## 2025-09-13
- chore(debug): Add canary print statement to tasks.py (commit: 5cb46c7)

## 2025-09-13
- feat(iam): Implement dedicated Batch Task Role for application permissions (commit: 23e3254)

## 2025-09-13
- style: Format Terraform code (commit: 5f5cf59)

## 2025-09-13
- chore(debug): Add network diagnostics tools and script for V11 test (commit: c9688bb)

## 2025-09-13
- fix(sfn): Use explicit full commands for Batch jobs (commit: f4af9ea)

## 2025-09-13
- fix(app): Harden boto3 client initialization with explicit region (commit: 999a8af)

## 2025-09-13
- fix(tf): Correct syntax error in SFN logging_configuration (commit: 2c6ffa5)

## 2025-09-13
- style: Format step_functions.tf (commit: 7f1a96a)

## 2025-09-13
- feat(batch): Use immutable Git SHA for image tag to defeat caching (commit: 5527407)

## 2025-09-13
- style: Format main.tf (commit: 0c1f3a5)

## 2025-09-13
- feat(cicd): Implement unified platform deployment workflow (commit: 1435288)

## 2025-09-13
- chore: Trigger the new unified deployment workflow (commit: aef48ae)

## 2025-09-13
- chore(debug): Enable boto3 debug logging in application (commit: e6f5896)

## 2025-09-13
- chore(debug): Run definitive canary test for CloudWatch metrics (commit: f0268d2)

## 2025-09-13
- refactor: Finalize and clean project repository structure (commit: 04901b7)

## 2025-09-13
- chore: Trigger the final unified deployment workflow (commit: d35ccb3)

## 2025-09-13
- refactor(tf): Consolidate data sources into a central data.tf file (commit: d358189)

## 2025-09-13
- fix(tf): Consolidate all SFN resources into step_functions.tf (commit: ac7a69d)

## 2025-09-13
- refactor!: Finalize project structure to a clean, cloud-native mon-orepo (commit: dc453dc)

## 2025-09-13
- chore(debug): Run minimal viable metric test (commit: 92646d5)

## 2025-09-13
- feat(app): Finalize metrics and restore production configuration (commit: a9b4090)

## 2025-09-13
- feat(metrics): Migrate to GeyserGenomicsV2 namespace and add dashboard (commit: 69664c0)

## 2025-09-14
- chore(metrics): Revert namespace back to original GeyserGenomics (commit: 4ff73b9)

## 2025-09-14
- chore(metrics): Revert namespace back to original GeyserGenomics (commit: 063ec81)

## 2025-09-14
- fix(dashboard) (commit: 3f80e97)

## 2025-09-14
- feat(dashboard): Implement dynamic dashboard via local-exec (commit: 7c2a670)

## 2025-09-14
- feat(dashboard): Implement dynamic dashboard via local-exec (commit: 90ce440)
