# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased] - 2025-06-17

### Changed
- **Major Architectural Pivot:** The project is moving from a stateful, persistent Airflow worker model with a shared volume to a fully stateless, cloud-native architecture.
- **New Core Technology:** AWS Batch will be introduced as the primary compute engine for running bioinformatics jobs.
- **New Design Principle:** Airflow will now act as a pure orchestrator, submitting jobs to AWS Batch rather than executing them directly. This better reflects modern, scalable, serverless design patterns.
- **Reasoning:** This pivot was made to eliminate the inefficiency of the "download/process/upload" pattern for every task. The new architecture ensures each job is an ephemeral, stateless task that reads from and writes to S3, which is significantly more scalable, resilient, and cost-effective.

