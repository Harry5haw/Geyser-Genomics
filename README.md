<div align="center">
  <h1>🌋 Geyser Genomics 🌋</h1>
  <p>
    <strong>A fully automated, cloud-native platform on AWS for scalable genomic data analysis.</strong>
  </p>
  
  <p>
    <a href="#️-project-status--features"><img src="https://img.shields.io/badge/Project%20Status-In%20Active%20Development-orange?style=for-the-badge" alt="Project Status"></a>
    <a href="../../actions/workflows/tests.yml"><img src="https://img.shields.io/badge/Tests-Offline%20%2B%20Docker%20E2E-brightgreen?style=for-the-badge" alt="Tests"></a>
  </p>

  <p>
    <a href="https://aws.amazon.com/"><img src="https://img.shields.io/badge/AWS-232F3E?style=for-the-badge&logo=amazon-aws&logoColor=white" alt="AWS"></a>
    <a href="https://www.terraform.io/"><img src="https://img.shields.io/badge/Terraform-7B42BC?style=for-the-badge&logo=terraform&logoColor=white" alt="Terraform"></a>
    <a href="https://github.com/features/actions"><img src="https://img.shields.io/badge/GitHub%20Actions-2088FF?style=for-the-badge&logo=github-actions&logoColor=white" alt="GitHub Actions"></a>
    <a href="https://aws.amazon.com/step-functions/"><img src="https://img.shields.io/badge/Step%20Functions-D45404?style=for-the-badge&logo=amazon-aws&logoColor=white" alt="AWS Step Functions"></a>
    <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python"></a>
    <a href="https://www.docker.com/"><img src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker"></a>
 
  </p>
</div>

<!-- This is your banner image. It will be centered automatically. -->
<div align="center">
  <img src="docs/assets/Github social media banner v9.png" alt="Technologies for DNA sequencing visualization" width="70%"/>
</div>

---

### 📋 Table of Contents

1.  [What is Geyser Genomics?](#-what-is-geyser-genomics) 
2.  [The Odyssey: Why This Project Exists](#-the-odyssey-why-this-project-exists)
3.  [The Challenge: From a DNA Sample to an Answer](#-the-challenge-from-a-dna-sample-to-an-answer)
4.  [The Impact: Key Benefits](#-the-impact-key-benefits)
5.  [Project Status & Features](#️-project-status--features)
6.  [How It Works: The Architecture](#️-how-it-works-the-architecture)
7.  [Technical Deep Dive](#-technical-deep-dive)
8.  [Run It Locally in 2 Minutes (No AWS)](#-run-it-locally-in-2-minutes-no-aws)
9.  [Deployment & Usage](#-deployment--usage)
10. [Roadmap](#-roadmap)

---

## 💡 What is Geyser Genomics?

Geyser Genomics is an **automated, serverless data processing platform**. Its purpose is to execute the complex scientific workflow that transforms raw, unreadable DNA data from a sequencing machine into a final, human-readable list of genetic variants.

It functions as a **robotic science lab in the cloud**. A scientist simply uploads their raw data file, and the platform automatically summons the necessary cloud resources, runs a multi-step bioinformatics analysis, and places the final results in a secure data lake, all with zero manual intervention.

**The Transformation: From Raw Data to Actionable Insight**

| Input | The Platform's Process (Automation) | Output |
| :--- | :--- | :--- |
| 🧬 **Raw Data File** <br> *(Millions of jumbled DNA fragments)* | 1. **Quality Control** on the raw data. <br> 2. **Alignment:** Compares data to a reference genome. <br> 3. **Variant Calling:** Identifies all differences. | 📄 **VCF File** <br> *(A clean list of genetic mutations)* |

The platform provides four key advantages:

*   ⚙️ **Fully Automated:** The entire pipeline is event-driven. An upload to S3 triggers the workflow, eliminating manual setup and human error.
*   🚀 **Infinitely Scalable:** Built on serverless AWS services like Step Functions and Fargate, the platform scales from zero to thousands of parallel jobs on demand, then scales back to zero.
*   💰 **Cost-Aware:** Compute is Fargate, which scales to zero, so analysis capacity costs nothing when idle. Supporting network infrastructure is being reworked so the platform's idle cost is close to zero too (see [Roadmap](#-roadmap)).
*   🔬 **Scientifically Rigorous:** Every tool is packaged in a version-controlled Docker container, and every change is tested against synthetic data with known mutations, so results are reproducible and verified.

---

## 🧭 The Odyssey: Why This Project Exists

This project was born from a profound, life-changing journey through the world of genomic medicine. It is a tribute to the pioneering work of NHS England and its **100,000 Genomes Project**.

> The NHS 100,000 Genomes Project is a landmark UK initiative aimed at sequencing the complete genetic codes of patients with rare diseases and their families. By creating this massive dataset, it seeks to uncover new diagnoses and pave the way for personalized medicine.

For eighteen years, my life was an odyssey through a fog of medical uncertainty with no linking diagnosis. As part of the project, my mother, my father, and I all donated our blood. Our entire genomes would be sequenced and explored, becoming three more data points in a vast ocean of information. We were told it was unlikely we would hear anything back.

<table align="center" border="0" cellspacing="0" cellpadding="10">
  <tr>
    <td align="center">
      <img src="docs/assets/Family_Samples.jpg" alt="DNA samples from my family for the project" width="300"/>
    </td>
    <td align="center">
      <img src="docs/assets/family_samples_2.jpg" alt="A closer view of the genomic samples" width="300"/>
    </td>
  </tr>
</table>

A year later, against all odds, I received a result. The project had delivered a link. In the three billion letters of my genetic code, they had found a single, ultra-rare mutation in the **COA3 gene**, causing a mitochondrial defect.

<table align="center" border="0" cellspacing="0" cellpadding="10">
  <tr>
    <td align="center">
      <img src="docs/assets/Mutation_highlight.jpg" alt="A DNA sequence with one letter highlighted in red to show a mutation" width="300"/>
    </td>
    <td align="center">
      <img src="docs/assets/Genome_Family_Tree.jpg" alt="A family tree showing the inheritance of the genetic mutation" width="300"/>
    </td>
  </tr>
</table>

The statistical probability of this discovery is staggering. The odds of both parents carrying the same rare recessive fault and passing it on is approximately **1 in 64 million**.

### The 1 in 64 Million Chance

This isn't a random number; it's grounded in a core concept of population genetics known as the **Hardy-Weinberg principle**. It's the scientific method for estimating how rare a genetic trait is. The principle is expressed with the equation:

$$ p^2 + 2pq + q^2 = 1 $$

This describes how genetic variations are distributed in a population, where `2pq` represents the probability of being a carrier—like my parents. The calculation itself is a straightforward product of these rare probabilities:

*   **🧑 Carrier Frequency:** The estimated chance of one person being a carrier (`2pq`) is **1 in 8,000**.
*   **🧑‍🤝‍🧑 Two Carriers Meet:** The probability of two such carriers meeting is therefore:

<p align="center">
  <code>(1 / 8,000)   ×   (1 / 8,000)   =   1 / 64,000,000</code>
</p>

My donated genome will now serve as a data point to help others, ensuring that a single-letter fault doesn't define another patient's life. My odyssey of discovery took two decades, but it doesn't need to be that way for others. With technologies like Geyser Genomics, we can accelerate these discoveries and build solutions that may one day repair the very code of life itself.

<table align="center" border="0" cellspacing="0" cellpadding="10">
  <tr>
    <td align="center" width="32%">
      <img src="docs/assets/helping_hands.jpg" alt="Hands reaching out to form a double helix, representing helping others."/>
      <br><em>We can help eachother</em>
    </td>
    <td align="center" width="32%">
      <img src="docs/assets/odyssey.jpg" alt="A person climbing a DNA strand, representing the long diagnostic journey."/>
      <br><em>By looking within ourselves</em>
    </td>
    <td align="center" width="32%">
      <img src="docs/assets/future_solutions.jpg" alt="DNA being knitted or repaired, representing future solutions."/>
      <br><em>To build a better future</em>
    </td>
  </tr>
</table>

---

## 🤔 The Challenge: From a DNA Sample to an Answer

Analyzing genomes is incredibly difficult. Scientists face three major hurdles:

*    **The Data Deluge:** A single human genome can be over 100 gigabytes. Analyzing hundreds requires a staggering amount of storage and power.
*    **The Complex Recipe:** The analysis involves dozens of specialized scientific tools, each needing to be run in a specific order with precise parameters. One mistake can invalidate the results.
*    **The Hardware Hurdle:** This work traditionally requires buying and maintaining powerful, expensive server clusters that are difficult to manage and often sit idle.

---

## ✨ The Impact: Key Benefits

| Benefit | How Geyser Genomics Delivers |
| :--- | :--- |
| **Blazing Speed** | By using thousands of on-demand cloud CPUs in parallel, analyses that took weeks can now be completed in a matter of hours. |
| **Unwavering Reliability** | The automated workflow and version-controlled containers eliminate human error, producing consistent and trustworthy results every time. |
| **Drastic Cost Reduction** | Compute is serverless and scales to zero, so you pay for processing power only while an analysis is running. |
| **Empowered Scientists** | Researchers can run massive analyses without needing to be cloud experts, freeing them to focus entirely on the science. |

---

## 🗺️ Project Status & Features

The core pipeline (FASTQ → QC → alignment → VCF) is built and verified end to end. The platform is in active development: the cloud deployment is being hardened for cost and security before new features are added.

| Feature | Status | Description |
| :--- | :---: | :--- |
| **Bioinformatics Pipeline** | ✅ Complete | Decompress → FastQC → BWA-MEM alignment (sorted BAM) → bcftools variant calling. |
| **Offline Development & Testing** | ✅ Complete | The full pipeline runs locally in Docker with no AWS account. A pytest suite plus a Docker end-to-end test checks that planted mutations in synthetic data are recovered exactly. Runs on every push. |
| **Infrastructure as Code** | ✅ Complete | The entire AWS environment is defined in Terraform. |
| **Serverless Orchestration** | ✅ Complete | AWS Step Functions runs the four stages in order, with failure notifications via SNS. |
| **Scalable Compute Engine** | ✅ Complete | AWS Batch on Fargate provides containerized, on-demand compute that scales to zero. |
| **Event-Driven Workflow** | ✅ Complete | Uploading a file to the S3 data lake starts the pipeline automatically. |
| **Secure CI/CD (OIDC)** | 🚧 Under Construction | GitHub Actions authenticates to AWS with short-lived OIDC credentials (no stored keys), with separate dev and prod roles. Automatic deploys are paused while this is finalised. |
| **Cost-Safe Networking** | 🚧 Under Construction | Removing the always-on NAT gateway so that an idle platform costs close to nothing. |
| **Custom Monitoring** | 🚧 Under Construction | Each stage emits duration and failure metrics to CloudWatch, shown on dashboards. |
| **Google DeepVariant** | 🗓️ Future Work | Deep-learning variant calling on GPU-backed AWS Batch. |
| **Web UI Front-End** | 🗓️ Future Work | A Django web application for uploading samples and browsing results. |

---

## 🛠️ How It Works: The Architecture

This platform is built on a modern, event-driven, serverless architecture. There are no virtual machines or servers to manage.

```mermaid
graph TD
    A[1. User uploads FASTQ file] --> B[Amazon S3 Data Lake];
    B -- S3 Event --> C[AWS Lambda Trigger];
    C -- Starts Execution --> D[AWS Step Functions State Machine];

    subgraph "Bioinformatics Workflow (Orchestrated by Step Functions)"
        D -- "Run Decompress" --> E[AWS Batch Job on Fargate];
        E -- "Reads/Writes Data" --> B;
        E -- "Success" --> F[AWS Batch Job on Fargate];
        F -- "Run Quality Control" --> G[AWS Batch Job on Fargate];
        G -- "Reads/Writes Data" --> B;
        G -- "Success" --> H[AWS Batch Job on Fargate];
        H -- "Run Align" --> I[AWS Batch Job on Fargate];
        I -- "Reads/Writes Data" --> B;
        I -- "Success" --> J[AWS Batch Job on Fargate];
        J -- "Run Call Variants" --> K[Final VCF Result];
        K --> B;
    end
    
    subgraph "CI/CD Pipeline (GitHub Actions)"
        L[git push] --> T[Offline tests: unit + Docker end-to-end];
        T --> M{Build, Push & Deploy via OIDC};
        M --> N[Amazon ECR];
        M --> D;
    end
    
    subgraph "Observability"
        E -- "Emit Custom Metrics" --> O[Amazon CloudWatch];
        G -- "Emit Custom Metrics" --> O;
        I -- "Emit Custom Metrics" --> O;
        J -- "Emit Custom Metrics" --> O;
        O --> P[CloudWatch Dashboard];
    end

    style A fill:#4CAF50,stroke:#333,stroke-width:2px,color:#fff
    style K fill:#9C27B0,stroke:#333,stroke-width:2px,color:#fff
```

---

## 🤖 Technical Deep Dive

The platform's design is guided by modern cloud-native principles:

| Technology | Role & Rationale |
| :--- | :--- |
| **Terraform** | **The Cloud Architect.** Defines all AWS resources (VPC, S3, IAM, ECR, Batch) as code. This ensures a consistent, repeatable, and secure deployment every time. |
| **AWS Step Functions** | **The Serverless Conductor.** Replaces the operational burden of self-hosting an orchestrator like Airflow. It translates the scientific pipeline into a resilient, managed state machine, handling state, error handling, and parallelism. |
| **AWS Batch + Fargate** | **The Serverless Compute Engine.** Manages a fleet of on-demand, containerized compute resources. It handles job queuing, scheduling, and scaling from zero to thousands of CPUs, eliminating the need to manage a cluster. |
| **Docker** | **The Reproducible Toolbox.** Packages all bioinformatics tools (`FastQC`, `BWA`, `Samtools`, etc.) and their specific dependencies into a single, portable container image. This guarantees scientific reproducibility. |
| **GitHub Actions**| **The CI/CD Engine.** Every push runs the offline test suite. The deploy workflow builds the Docker image, pushes it to ECR and applies Terraform, authenticating with short-lived OIDC credentials instead of stored AWS keys. |
| **pytest + synthetic data**| **The Safety Net.** A generator creates a small reference genome and simulated reads with known mutations. The end-to-end test runs every stage in Docker and requires the final VCF to contain exactly those mutations. |
| **Amazon S3**| **The Data Lake.** Provides durable, scalable, and cost-effective storage for massive genomic datasets. It serves as the event source for the pipeline and the single source of truth for all data. |
| **Python**| The primary language for the bioinformatics application logic (`app/tasks.py`) and for interacting with AWS services via the `boto3` SDK. |

---

## 💻 Run It Locally in 2 Minutes (No AWS)

The whole pipeline runs on a laptop. Each stage runs in its own container from the same image the cloud uses, and a local folder (`local_bucket/`) stands in for the S3 data lake with the same layout.

**Prerequisites:** Docker and Python 3.11.

```bash
# 1. Build the pipeline image (FastQC, BWA, samtools, bcftools)
docker build -f Dockerfile -t geyser-app .

# 2. Create a synthetic genome and 30x reads with 10 known mutations planted in them
python scripts/make_test_data.py

# 3. Run all four stages in order, just as Step Functions would, then check the VCF against the truth
python scripts/run_local.py --sample SYNTH001 --reference synthetic_ref.fa --check
```

Expected result: `Truth check: 10/10 planted SNPs called, 0 unexpected calls`

**Tests:**

```bash
pip install -r requirements-dev.txt
pytest                                           # unit tests, no Docker needed
GEYSER_E2E=1 pytest tests/test_e2e_docker.py     # full pipeline in Docker
```

The tests also check that the stage list in the Step Functions definition (`infrastructure/step_functions.tf`), the stage code (`app/tasks.py`) and the local runner all agree.

The storage and metrics backends are chosen by environment variables, so the same code runs locally and in AWS:

| Variable | Values | Default |
| :--- | :--- | :--- |
| `STORAGE_BACKEND` | `s3`, `local` | `s3` |
| `LOCAL_STORAGE_DIR` | path to the local data lake | `local_bucket` |
| `METRICS_BACKEND` | `cloudwatch`, `local`, `none` | `cloudwatch` for S3, `local` otherwise |

---

## 🚀 Deployment & Usage

### 1. The Deployment Process
The infrastructure is deployed by the GitHub Actions workflow `.github/workflows/deploy-platform.yml`, which builds the image, pushes it to ECR and runs Terraform. It authenticates to AWS with GitHub OIDC (short-lived credentials, no stored keys), and separate IAM roles are scoped to the `dev` and `prod` GitHub environments.

> **Note:** automatic deployment is currently paused while the networking is reworked for cost. All development and testing happens offline (see above).

**Prerequisites for CI/CD:**
*   An AWS Account, with the GitHub OIDC identity provider configured.
*   GitHub environments `dev` and `prod`.
*   The following secrets configured in the GitHub repository settings:
    *   `AWS_REGION`: The target AWS region (e.g., `eu-west-2`).
    *   `ECR_IAM_ROLE_ARN`: The ARN of the IAM role for building and pushing to ECR.
    *   `TERRAFORM_IAM_ROLE_ARN`: The ARN of the IAM role for running Terraform.

### 2. How to Use the Platform (Event-Driven)
Once deployed, using the platform is as simple as uploading a file.

1.  **Locate the S3 Data Lake:** Find the S3 bucket created by Terraform. Its name has the format `<project_name>-data-lake-<random-suffix>`.
2.  **Upload Raw Data:** Upload your gzipped FASTQ file to the `raw_reads/` prefix, e.g. `raw_reads/SRR062634.fastq.gz`. The sample ID is the part of the filename before the first dot.
3.  **Monitor the Execution:** The upload automatically starts the Step Functions state machine. You can follow its progress in the AWS Step Functions console.
4.  **Retrieve Results:** When the run succeeds, the outputs are in the same bucket:

| Stage | Output |
| :--- | :--- |
| Decompress | `decompressed/<id>.fastq` |
| Quality control | `qc_reports/<id>_fastqc.html` and `.zip` |
| Alignment | `alignments/<id>.bam` (coordinate-sorted) |
| Variant calling | `variants/<id>.vcf.gz` |

### 3. Dashboard Management
The custom CloudWatch Dashboard provides real-time performance metrics for the pipeline. Due to the complexity of its JSON definition and the unreliability of managing it via Terraform, this resource is managed by a dedicated Python script.

To deploy or update the dashboard, run the following command from the project root:
```bash
python scripts/deploy_dashboard.py
```

---

## 🧭 Roadmap

1. **Cost-safe cloud deployment:** make the NAT gateway optional (off by default), run Fargate jobs in public subnets, set a lifecycle policy on ECR and short log retention, and add an AWS Budget alert.
2. **CI/CD hardening:** finish the OIDC role separation, restrict `prod` to `main` with required review, and make deploys manual rather than automatic on every push.
3. **Monitoring:** finish the CloudWatch dashboards for stage durations and failures.
4. **Google DeepVariant:** an alternative variant-calling stage on GPU-backed AWS Batch.
5. **Web UI:** a Django front end for uploading samples and browsing results.
