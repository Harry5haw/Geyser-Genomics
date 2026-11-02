<div align="center">

<img src="docs/assets/Github social media banner v9.png" alt="Geyser Genomics banner: technologies for DNA sequencing" width="85%"/>

<h1>🌋 Geyser Genomics</h1>

<p><strong>An event-driven AWS pipeline that turns raw DNA sequencing reads into a list of genetic variants,<br/>built and tested offline first.</strong></p>

<p>
  <a href="#️-project-status--features"><img src="https://img.shields.io/badge/Project%20Status-In%20Active%20Development-orange?style=for-the-badge" alt="Project status: in active development"></a>
</p>

<p>
  <img src="https://img.shields.io/badge/AWS-232F3E?style=for-the-badge&logo=amazon-aws&logoColor=white" alt="AWS">
  <img src="https://img.shields.io/badge/Step%20Functions-D45404?style=for-the-badge&logo=amazon-aws&logoColor=white" alt="AWS Step Functions">
  <img src="https://img.shields.io/badge/Batch%20%2B%20Fargate-FF9900?style=for-the-badge&logo=amazon-aws&logoColor=white" alt="AWS Batch and Fargate">
  <img src="https://img.shields.io/badge/Terraform-7B42BC?style=for-the-badge&logo=terraform&logoColor=white" alt="Terraform">
  <br/>
  <img src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Django-092E20?style=for-the-badge&logo=django&logoColor=white" alt="Django">
  <img src="https://img.shields.io/badge/CUDA%20GPU-76B900?style=for-the-badge&logo=nvidia&logoColor=white" alt="NVIDIA CUDA">
  <img src="https://img.shields.io/badge/GitHub%20Actions-2088FF?style=for-the-badge&logo=github-actions&logoColor=white" alt="GitHub Actions">
</p>

<p>
  <a href="#-run-it-locally-in-2-minutes-no-aws"><b>🚀 Quickstart</b></a> &nbsp;·&nbsp;
  <a href="#️-how-it-works-the-architecture"><b>🏗️ Architecture</b></a> &nbsp;·&nbsp;
  <a href="#️-project-status--features"><b>🗺️ Status</b></a> &nbsp;·&nbsp;
  <a href="#deepvariant-on-a-local-nvidia-gpu"><b>🧠 DeepVariant</b></a> &nbsp;·&nbsp;
  <a href="#️-roadmap"><b>🛣️ Roadmap</b></a>
</p>

<table>
  <tr>
    <td align="center" width="20%"><h3>4</h3>containerised<br/>pipeline stages</td>
    <td align="center" width="20%"><h3>10 / 10</h3>planted SNPs recovered<br/>on every push</td>
    <td align="center" width="20%"><h3>0.9977</h3>DeepVariant SNP F1<br/>on HG003 chr20*</td>
    <td align="center" width="20%"><h3>3.3×</h3>faster neural-network step<br/>on an RTX 3080 GPU</td>
    <td align="center" width="20%"><h3>0</h3>stored AWS keys<br/>in CI (OIDC)</td>
  </tr>
</table>

<sub>* hap.py (vcfeval, PASS only) against GIAB v4.2.1 confident regions, the same method as Google's case studies. Linear and pangenome-aware DeepVariant score the same here. See <a href="#deepvariant-on-a-local-nvidia-gpu">DeepVariant</a>.</sub>

</div>

---

### 📋 Table of Contents

1.  [What is Geyser Genomics?](#-what-is-geyser-genomics) 
2.  [The Odyssey: Why This Project Exists](#-the-odyssey-why-this-project-exists)
3.  [The Challenge: From a DNA Sample to an Answer](#-the-challenge-from-a-dna-sample-to-an-answer)
4.  [Design Goals and Where They Stand](#-design-goals-and-where-they-stand)
5.  [Project Status & Features](#️-project-status--features)
6.  [How It Works: The Architecture](#️-how-it-works-the-architecture)
7.  [Technical Deep Dive](#-technical-deep-dive)
8.  [Run It Locally in 2 Minutes (No AWS)](#-run-it-locally-in-2-minutes-no-aws)
9.  [Web UI](#️-web-ui)
10. [Deployment & Usage](#-deployment--usage)
11. [Roadmap](#️-roadmap)

---

## 💡 What is Geyser Genomics?

Geyser Genomics is an **event-driven bioinformatics pipeline**. It takes raw DNA data from a sequencing machine (a gzipped FASTQ file) and turns it into a list of genetic variants (a VCF file).

In the cloud design, uploading a file to S3 starts the run. AWS Step Functions then runs each stage as a container on AWS Batch, and the results are written back to the same bucket. The same containers also run on a laptop against a local folder, which is how the project is developed and tested.

**From raw reads to variants**

```mermaid
flowchart LR
    IN(["🧬 FASTQ.gz<br/>millions of raw reads"]) --> D["📦 Decompress"]
    D --> Q["🔍 Quality control<br/>FastQC"]
    Q --> A["🧭 Align to reference<br/>BWA-MEM + samtools sort + markdup"]
    A --> V["🧪 Call variants<br/>bcftools"]
    V --> OUT(["📄 VCF.gz<br/>list of variants"])
    A -.->|"alternative caller"| DV["🧠 DeepVariant<br/>GPU"]
    DV -.-> OUT2(["📄 VCF.gz"])
    Q -.-> R(["📊 QC report"])

    classDef io fill:#6A1B9A,stroke:#4A148C,color:#fff
    classDef stage fill:#E65100,stroke:#BF360C,color:#fff
    classDef alt fill:#2E7D32,stroke:#1B5E20,color:#fff
    class IN,OUT,OUT2,R io
    class D,Q,A,V stage
    class DV alt
```

What it does today:

*   ⚙️ **Event-driven:** an upload to the `raw_reads/` prefix starts the workflow. No one has to launch the stages by hand.
*   📦 **Containerised and on demand:** every stage runs in the same Docker image on AWS Batch (Fargate). Compute only runs while a job is running. The Fargate environment is capped at 16 vCPUs, which is up to 8 jobs at once.
*   💰 **Cost-conscious:** there is no always-on NAT gateway (it is optional and off by default). ECR images and logs are pruned automatically, and an AWS Budget alert fires at $10 a month. Idle cost is mostly storage.
*   🧪 **Tested offline:** every push runs the unit tests and a Docker end-to-end run on synthetic data with known mutations. The run must recover exactly those mutations.

> [!NOTE]
> **Where it stands:** the pipeline is verified locally in Docker. The AWS infrastructure is defined in Terraform and deployed, but the cloud pipeline has not been run end to end since the cost and security changes of November 2026. The next milestone is pangenome-aware variant calling, built and benchmarked locally first.

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

## ✨ Design Goals and Where They Stand

| Goal | Approach | Where it is today |
| :--- | :--- | :--- |
| **Reproducible results** | All tools pinned in one Docker image; synthetic data with planted mutations as a truth check | Done. The end-to-end test runs on every push. |
| **No manual steps** | An S3 upload triggers Lambda, which starts Step Functions | Built in Terraform. Not yet re-tested in the cloud. |
| **Pay only while running** | Batch on Fargate, no NAT gateway, budget alert | Applied. Storage is the remaining idle cost. |
| **Scale out across samples** | Each upload is its own Step Functions execution | Limited by design: one sample per run, stages in sequence, up to 8 Fargate jobs at once. |
| **Accessible to non-cloud users** | A web UI for uploading samples and browsing results | Works offline against the local folder. Not yet connected to S3 or deployed. |

---

## 🗺️ Project Status & Features

The core pipeline (FASTQ → QC → alignment → VCF) is built and verified end to end **locally in Docker**. The AWS side is defined and deployed, and has just been hardened for cost and security. It has not been run end to end since then.

| Feature | Status | Description |
| :--- | :---: | :--- |
| **Bioinformatics Pipeline** | ✅ Verified locally | Decompress → FastQC → BWA-MEM alignment (sorted, duplicate-marked BAM) → bcftools variant calling. |
| **Offline Development & Testing** | ✅ Complete | The full pipeline runs locally in Docker with no AWS account. A pytest suite plus a Docker end-to-end test checks that planted mutations in synthetic data are recovered exactly. Runs on every push. |
| **Infrastructure as Code** | ✅ Complete | The project's AWS resources are defined in Terraform. Changes are planned and applied by hand after review. |
| **Orchestration & Trigger** | 🟡 Built, cloud re-test pending | S3 upload → Lambda → Step Functions runs the four stages in order on AWS Batch (Fargate), with failure notifications via SNS. |
| **CI Authentication (OIDC)** | ✅ Complete | GitHub Actions gets short-lived AWS credentials through OIDC, with no stored keys. Separate dev and prod roles; the Terraform roles are read-only, and prod only trusts the `main` branch. |
| **Automated Deployment** | ⏸️ Not yet | CI runs tests only. The Terraform workflow can only plan, is manual, and is currently disabled. Deploys are done locally. |
| **Cost Safety** | ✅ Applied | NAT gateway optional and off, Fargate jobs in a public subnet, ECR and log retention limits, $10 monthly budget alert. |
| **Custom Monitoring** | 🚧 In progress | Each stage emits duration and failure metrics (CloudWatch, or a local file offline). The dashboards are scripted but not finished. |
| **Google DeepVariant** | 🚧 Local GPU working, cloud pending | Deep-learning variant calling as an alternative to bcftools. It runs locally on an NVIDIA GPU in Docker, with accuracy and timings measured (see below). A GPU Batch environment exists in AWS, but the stage is not wired into Step Functions and needs larger instances. |
| **Web UI Front-End** | ✅ Works offline | A Django app for uploading samples, generating demo data and browsing results (FastQC, variants, downloads). Runs locally; not deployed. See [Web UI](#️-web-ui). |

---

## 🛠️ How It Works: The Architecture

The design is event-driven and uses managed AWS services, so there are no servers to look after for the main pipeline. (The planned DeepVariant stage uses GPU instances that AWS Batch starts and stops.)

```mermaid
flowchart TB
    U(["📤 Upload<br/>raw_reads/ID.fastq.gz"]) --> S3[("🪣 Amazon S3<br/>data lake")]
    S3 -->|"S3 event<br/>prefix raw_reads/, suffix .gz"| L["λ Lambda trigger"]
    L -->|"StartExecution"| SF1

    subgraph SFN ["⚙️ AWS Step Functions: one execution per sample"]
        direction LR
        SF1["Decompress"] --> SF2["Quality control"] --> SF3["Align"] --> SF4["Call variants"]
    end

    SFN ==>|"submitJob.sync<br/>one job per stage"| BATCH["🐳 AWS Batch on Fargate<br/>geyser-app image · 2 vCPU / 4 GB"]
    ECR[("📦 Amazon ECR")] -.->|"image"| BATCH
    BATCH <-->|"read inputs<br/>write outputs"| S3
    SFN -.->|"any failure"| SNS["📣 SNS alert"]
    BATCH -.->|"Duration, FailureCount"| CW["📈 CloudWatch metrics"]
    CW -.-> DASH["Dashboards<br/>in progress"]

    subgraph GHA ["🔧 GitHub Actions"]
        direction LR
        PUSH["push / pull request"] --> TESTS["✅ Unit + Docker E2E tests"]
        MAN["manual run"] -.-> PLAN["📝 terraform plan<br/>OIDC · read-only role"]
    end

    classDef user fill:#2E7D32,stroke:#1B5E20,color:#fff
    classDef store fill:#6A1B9A,stroke:#4A148C,color:#fff
    classDef aws fill:#E65100,stroke:#BF360C,color:#fff
    classDef obs fill:#1565C0,stroke:#0D47A1,color:#fff
    classDef pending fill:#9E9E9E,stroke:#616161,color:#fff,stroke-dasharray: 4 3
    class U user
    class S3,ECR store
    class L,SF1,SF2,SF3,SF4,BATCH,SNS aws
    class CW,TESTS,PLAN obs
    class DASH pending
```

---

## 🤖 Technical Deep Dive

The platform's design is guided by modern cloud-native principles:

| Technology | Role & Rationale |
| :--- | :--- |
| **Terraform** | **The Cloud Architect.** Defines the AWS resources (VPC, S3, IAM, ECR, Batch, Step Functions, Lambda) as code, so the environment can be reviewed in a plan before anything changes. |
| **AWS Step Functions** | **The Conductor.** Replaced a self-hosted Airflow setup from earlier in the project. A managed state machine runs the stages in order and routes any failure to an SNS notification. |
| **AWS Batch + Fargate** | **The Compute Engine.** Runs each stage as a container job (2 vCPU / 4 GB) with queuing and scheduling handled by AWS. The compute environment is capped at 16 vCPUs, which keeps costs bounded. |
| **Docker** | **The Reproducible Toolbox.** Packages all bioinformatics tools (`FastQC`, `BWA`, `Samtools`, etc.) and their specific dependencies into a single, portable container image. This guarantees scientific reproducibility. |
| **GitHub Actions**| **The CI Engine.** Every push and pull request runs the offline test suite. A manual workflow can run `terraform plan` with short-lived OIDC credentials and a read-only role. It cannot apply changes. |
| **pytest + synthetic data**| **The Safety Net.** A generator creates a small reference genome and simulated reads with known mutations. The end-to-end test runs every stage in Docker and requires the final VCF to contain exactly those mutations. |
| **Amazon S3**| **The Data Lake.** Holds the inputs, every intermediate file and the results, under fixed key prefixes. An upload to `raw_reads/` is the event that starts the pipeline. |
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

# Paired-end instead: writes PAIRED001_R1.fastq.gz and PAIRED001_R2.fastq.gz
python scripts/make_test_data.py --sample PAIRED001 --paired
python scripts/run_local.py --sample PAIRED001 --reference synthetic_ref.fa --check
```

> [!TIP]
> Expected result: `Truth check (bcftools): 10/10 planted SNPs called, 0 unexpected calls`
>
> `--duplicate-rate 0.1` plants exact read (or pair) copies, like PCR duplicates. The align stage's `samtools markdup` flags them, and the SNP calls stay the same.

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
| `S3_ENDPOINT_URL` | an S3-compatible endpoint (OVHcloud Object Storage, MinIO, LocalStack) instead of AWS | unset (AWS) |
| `S3_ADDRESSING_STYLE` | `path`, `virtual`, `auto` | unset (botocore's `auto`) |
| `METRICS_BACKEND` | `cloudwatch`, `local`, `none` | `cloudwatch` for S3 on AWS, `none` with `S3_ENDPOINT_URL`, `local` otherwise |

### DeepVariant on a local NVIDIA GPU

[Google DeepVariant](https://github.com/google/deepvariant) `1.10.0` is available as an alternative variant caller, in its own image (`app-deepvariant/`). It reads the same `alignments/<id>.bam` and writes `variants_deepvariant/<id>.vcf.gz`, so its calls can be compared with the bcftools calls.

#### Why the reference genome matters: pangenomes

<div align="center">
  <img src="docs/assets/pangenome.gif" alt="Animation: a single linear reference genome built from a few people, compared with a pangenome reference that combines many diverse genomes" width="85%"/>
  <br/>
  <sub>Animation: Google Research, from <a href="https://research.google/blog/building-better-pangenomes-to-improve-the-equity-of-genomics/"><i>Building better pangenomes to improve the equity of genomics</i></a> (Andrew Carroll and Kishwar Shafin, May 2023).</sub>
</div>

<br/>

Variant callers like DeepVariant find variants by comparing reads with a **reference genome**. The standard reference is a single linear sequence, so variation it doesn't contain is easy to miss or to call wrongly. A study of 910 African individuals found about 300 million base pairs, roughly 10% of the reference, that were present in at least one person but absent from the linear reference.

A **pangenome** reference is a graph that combines many people's genomes. The Human Pangenome Reference Consortium's first draft (*Nature*, 2023) combines 47 genomes chosen to better reflect global diversity. Google and UC Santa Cruz built a graph-based variant-calling workflow around DeepVariant and reported improved accuracy across several thousand samples.

> [!NOTE]
> **Where this project is:**
> - **Done:** pangenome-aware variant calling (Google's pangenome-aware DeepVariant with the HPRC graph) runs **locally on the GPU** and is benchmarked on HG003 chr20 (results below).
> - **Still linear:** reads are aligned to GRCh38 with BWA-MEM (not to the graph).
> - **Not yet:** whole-genome graphs, other samples, and runs in the cloud. These are next on the [Roadmap](#️-roadmap) (#51).

#### Running it

```bash
docker run --rm --gpus all nvidia/cuda:12.3.2-base-ubuntu22.04 nvidia-smi   # GPU visible to Docker?
docker build -f app-deepvariant/Dockerfile -t geyser-deepvariant .
python scripts/run_local.py --sample SYNTH001 --reference synthetic_ref.fa --caller both --check

# Real data: NA12878, chr20:10.0-10.1 Mb, with the GIAB truth set (~5 MB download)
python scripts/fetch_dv_testdata.py
python scripts/run_local.py --sample NA12878_S1 --reference ucsc.hg19.chr20.unittest.fasta \
    --caller both --skip-to-variants --check

# Whole chromosome: HG003, NovaSeq 35x, all of chr20 on GRCh38, with GIAB v4.2.1 truth (~1.34 GB download)
python scripts/fetch_dv_testdata.py --dataset hg003-chr20
python scripts/run_local.py --sample HG003_chr20 --reference GRCh38_chr20.fa \
    --caller deepvariant --skip-to-variants --check --dv-memory 12g

# Pangenome-aware DeepVariant (GPU image) with the HPRC v1.1 graph cut down to chr20 (built locally with vg)
docker build -f app-deepvariant/Dockerfile --build-arg DV_TAG=pangenome_aware_deepvariant-1.10.0-gpu \
    -t geyser-deepvariant-pangenome .
python scripts/fetch_dv_testdata.py --dataset hg003-chr20 --pangenome
DEEPVARIANT_SHARDS=6 DEEPVARIANT_EXTRA_ARGS="--call_variants_extra_args=batch_size=256" \
python scripts/run_local.py --sample HG003_chr20 --reference GRCh38_chr20.fa \
    --caller pangenome --skip-to-variants --dv-memory 14g

# Score every caller's VCF with hap.py (vcfeval engine, PASS only, GIAB confident regions)
python scripts/benchmark_happy.py --sample HG003_chr20 --reference GRCh38_chr20.fa --region chr20
```

**Accuracy: HG003 chr20, hap.py.** HG003 is NovaSeq PCR-free 35x, scored against GIAB v4.2.1 confident regions. These are the same settings Google's case studies use (`--engine=vcfeval --pass-only`):

| Caller | Type | Missed (FN) | False (FP) | Recall | Precision | F1 |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: |
| bcftools | SNP | 361 | 835 | 0.9949 | 0.9882 | 0.9915 |
| bcftools | Indel | 582 | 487 | 0.9452 | 0.9562 | 0.9507 |
| DeepVariant 1.10 (linear) | SNP | 263 | 52 | 0.9963 | 0.9993 | **0.99775** |
| DeepVariant 1.10 (linear) | Indel | 48 | 15 | 0.9955 | 0.9986 | 0.99706 |
| Pangenome-aware DeepVariant 1.10 | SNP | **242** | 78 | **0.9966** | 0.9989 | 0.99772 |
| Pangenome-aware DeepVariant 1.10 | Indel | **42** | 18 | **0.9960** | 0.9984 | **0.99721** |

- **The setup reproduces Google's published result.** Their pangenome-aware BWA case study on the same BAM reports SNP F1 0.997746 and indel F1 0.996788 (version 1.9, whole-genome graph). Here: 0.997718 and 0.997206, with a chr20-only graph and version 1.10.
- **The pangenome finds variants the linear reference misses.** Missed SNPs fall by 8% (263 → 242) and missed indels by 13% (48 → 42).
- **It also makes slightly more false calls** (SNPs 52 → 78). On this one sample the total error count is level: 378 for linear against 380 for pangenome-aware.
- **HG003 is a hard case for showing a benefit.** It is Ashkenazi Jewish, an ancestry the standard reference already represents well. The preprint reports the largest gains with more diverse samples, whole genomes (most segmental duplications are on other chromosomes) and reads aligned to the graph. Those benchmarks are next.
- **bcftools is much better than earlier exact-match counts suggested** (SNP F1 0.99, not 0.87). Exact (position, ref, alt) matching without normalisation heavily penalised its indel representation.

Small datasets (exact-match counts): synthetic data, 10/10 planted SNPs and 0 extra calls for both callers; NA12878 slice, bcftools 45 TP / 4 FN / 4 FP and DeepVariant 49 TP / 0 FN / 0 FP. DeepVariant's accuracy is identical on GPU and CPU.

**Pangenome-aware DeepVariant: time and memory** (HG003 chr20, RTX 3080 10 GB, 16 CPUs, 6 `make_examples` workers):

| Step | Time | Notes |
| :--- | ---: | :--- |
| `make_examples` (CPU) | 936 s | Pileups from the reads and from the pangenome haplotypes at every candidate site |
| `call_variants` (GPU) | 257 s | 231,085 examples, about 3× linear DeepVariant's 79,759. Pangenome mode has no small model, so every candidate reaches the network. |
| `postprocess_variants` | 21 s | |
| **Stage total** | **1,241 s (20.7 min)** | |

- **Graph size drives memory.** HPRC's whole-genome graph (3.29 GB file) takes about 10.7 GB of shared memory once loaded, plus about 2 GB per `make_examples` worker. That ran out of memory in a 22 GB container. The chr20 graph built by `fetch_dv_testdata.py` (72 MB, same 46 samples / 90 haplotypes) loads in under a second, and the container peaked at about 8.4 GB.
- **GPU memory.** At the default batch size (1,024), `call_variants` ran out of memory on the 10 GB card (it tried to allocate 2.7 GB blocks). At batch 256 it fits. A 24 GB cloud GPU (A10G, L4) should run the default.
- **Known issue.** After the out-of-memory error, Google's wrapper hung instead of exiting. In the cloud, the stage needs a job timeout.

**Timing: whole chromosome.** HG003 chr20, 35x, measured on an RTX 3080 (10 GB) under Docker Desktop/WSL2 with 16 CPUs, one run per configuration. These are times for the DeepVariant stage only:

| Device | Small model | Examples sent to the neural network | make_examples (CPU) | call_variants (GPU step) | postprocess (CPU) | Stage total |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: |
| GPU | on (default) | 79,759 | 298.6 s | **128.1 s** | 17.5 s | **466.7 s** (7.8 min) |
| CPU | on (default) | 79,759 | 306.8 s | **426.5 s** | 21.6 s | **776.4 s** (12.9 min) |
| GPU | off | 222,978 | 303.5 s | 266.5 s | 20.5 s | 609.9 s (10.2 min) |
| CPU | off | 222,978 | 336.4 s | 1,200.5 s | 16.9 s | 1,575.4 s (26.3 min) |

```mermaid
xychart-beta
    title "DeepVariant stage time on HG003 chr20 (minutes, lower is better)"
    x-axis ["GPU", "CPU", "GPU, no small model", "CPU, no small model"]
    y-axis "Minutes" 0 --> 30
    bar [7.8, 12.9, 10.2, 26.3]
```

**At whole-chromosome scale the GPU pays off.**
- With default settings, `call_variants` is **3.3× faster** on the GPU and the whole stage is **1.7× faster**. With the small model off, `call_variants` is 4.5× faster: about 840 examples/s on the GPU against about 190 on the CPU.
- The stage gains less than `call_variants` does, because `make_examples` (about 300 s) runs on the CPU on either image. It is the largest step on the GPU.
- The small model is itself a CPU speed-up: it makes 64% of the calls before the neural network is involved.
- bcftools took 409 s for the same chromosome.

The CPU runs used a 12 GB container cap (`--dv-memory 12g`) and `--call_variants_extra_args=batch_size=256`. Without a cap, CPU `call_variants` took most of the 16 GB Docker VM and starved Windows. An 8 GB cap was too small for 16 `make_examples` shards.

<details>
<summary><b>Timing on small inputs (where the GPU is slower)</b></summary>

<br/>

Same machine, median of 3 runs:

| Data | Device | Small model | Examples sent to the neural network | make_examples (CPU) | call_variants (GPU step) | postprocess (CPU) | Stage total |
| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: |
| Synthetic 50 kb | GPU | on | 0 | 13.8 s | 4.7 s | 6.1 s | 27.7 s |
| Synthetic 50 kb | CPU | on | 0 | 15.0 s | 4.0 s | 6.0 s | 27.6 s |
| Synthetic 50 kb | GPU | off | 10 | 13.2 s | 18.7 s | 5.9 s | 41.1 s |
| Synthetic 50 kb | CPU | off | 10 | 12.2 s | 12.3 s | 5.7 s | 32.9 s |
| NA12878 100 kb | GPU | on | 90 | 33.6 s | 18.5 s | 5.7 s | 61.2 s |
| NA12878 100 kb | CPU | on | 90 | 34.2 s | 13.4 s | 5.6 s | 56.5 s |
| NA12878 100 kb | GPU | off | 305 | 33.3 s | 20.0 s | 6.6 s | 63.3 s |
| NA12878 100 kb | CPU | off | 305 | 32.9 s | 14.3 s | 5.7 s | 56.2 s |

**On small inputs the GPU is 5–6 s slower than the CPU.** With at most a few hundred examples, `call_variants` time is almost entirely fixed start-up: loading the model and initialising CUDA and cuDNN. The GPU's start-up costs more than TensorFlow's on the CPU. On the synthetic data, the small model makes every call, so the neural network sees nothing. `nvidia-smi` sampling confirmed the GPU was used: utilisation peaked at 98%, and TensorFlow reserved nearly all free VRAM.

</details>

**Tests.** `GEYSER_GPU_E2E=1 pytest tests/test_e2e_deepvariant.py` runs the GPU image. `GEYSER_DV_E2E=1` runs the CPU image; CI runs that one on pull requests, because GitHub runners have no GPU. Setting `GEYSER_DV_TESTDATA=<bucket>` also runs the real slice. In the cloud, a GPU Batch environment exists, but the stage is not wired into Step Functions yet, and its instances are too small for a whole chromosome. See the sizing notes in [`app-deepvariant/README.md`](app-deepvariant/README.md).

---

## 🖥️ Web UI

A small Django front end in `web/`. You can upload a gzipped FASTQ, or click **Generate demo sample** to create synthetic reads with known SNPs. Each stage turns green as it finishes. You then get the FastQC report, a table of called variants (with a "10/10 planted SNPs recovered" check for demo samples) and download links for every intermediate file.

```bash
docker build -f Dockerfile -t geyser-app .          # once: the pipeline image
pip install -r requirements-dev.txt                 # includes web/requirements.txt
python web/dev.py                                   # UI + local worker on http://127.0.0.1:8000
```

**How it fits the architecture.** The UI only reads and writes storage keys, through `app/storage.py`, and never runs the pipeline itself. An upload is stored at `raw_reads/<id>.fastq.gz`, or at `raw_reads/<id>_R1.fastq.gz` and `_R2.fastq.gz` when a second (R2) file is given. That key is the trigger, and a paired-end sample only starts once both mates exist:

| | Cloud | Offline |
| :--- | :--- | :--- |
| Storage | S3 (`STORAGE_BACKEND=s3`) | `local_bucket/` folder |
| Trigger | S3 event → Lambda → Step Functions | `manage.py runworker` polls `raw_reads/` → `scripts/run_local.py` |
| Stage status | from the output keys | from the output keys + `metrics/metrics.jsonl` + `runs/<id>.json` |

There is no database. Status is worked out from which outputs exist, the local metrics file and the worker's run records (`runs/<id>.json` and `.log`). The same UI could therefore run against the S3 bucket with no worker.

<details>
<summary><b>Settings, safety and tests</b></summary>

<br/>

**Settings** (environment variables): `DJANGO_DEBUG` (default **off**), `DJANGO_SECRET_KEY` (required when DEBUG is off), `DJANGO_ALLOWED_HOSTS`, `DJANGO_HTTPS=1` behind TLS (secure cookies, HSTS, redirect), `GEYSER_MAX_UPLOAD_MB` (default 200), `REFERENCE_NAME` for the worker (default `synthetic_ref.fa`), and `GEYSER_RUNNER=docker|host`. The storage variables are the same as the pipeline's, except that the UI defaults to `local`.

**Safety.** CSRF protection is on. Sample IDs are restricted to `[A-Za-z0-9_-]` because they become storage keys. Uploads must be gzip and within the size limit, and a sample can't be overwritten. Downloads are limited to a fixed list of file types. The FastQC report is shown in a sandboxed iframe.

**Tests:** `pytest` runs the web tests too (`web/tests/`, Django test client, temporary storage folder, no Docker).

</details>

---

## 🚀 Deployment & Usage

### 1. The Deployment Process
Deployment is manual. Terraform is run locally from `infrastructure/`: run `terraform plan`, review it, then `terraform apply`. The container image is built locally and pushed to ECR. There is no automatic deploy.

The GitHub Actions workflow `.github/workflows/deploy-platform.yml` ("Terraform Plan") can run a plan for the `dev` or `prod` environment. It authenticates with GitHub OIDC (short-lived credentials, no stored keys) using a read-only role, so it cannot change anything. It is manual and currently disabled.

<details>
<summary><b>Prerequisites for the CI plan workflow</b></summary>

<br/>

*   An AWS Account, with the GitHub OIDC identity provider configured.
*   GitHub environments `dev` and `prod`.
*   The following secrets configured in the GitHub repository settings:
    *   `AWS_REGION`: The target AWS region (e.g., `eu-west-2`).
    *   `ECR_IAM_ROLE_ARN`: The ARN of the IAM role for pushing to ECR.
    *   `TERRAFORM_IAM_ROLE_ARN`: The ARN of the read-only IAM role for `terraform plan`.

</details>

### 2. How to Use the Platform (Event-Driven)
Once deployed, a run is started by uploading a file.

> [!IMPORTANT]
> This cloud flow has not been re-tested since the November 2026 cost and security changes. The first cloud run is on the [Roadmap](#️-roadmap).

1.  **Locate the S3 Data Lake:** Find the S3 bucket created by Terraform. Its name has the format `<project_name>-data-lake-<random-suffix>`.
2.  **Upload Raw Data:** Upload your gzipped FASTQ file to the `raw_reads/` prefix, e.g. `raw_reads/SRR062634.fastq.gz`. The sample ID is the part of the filename before the first dot.
    *   **Paired-end:** upload both mates as `raw_reads/<id>_R1.fastq.gz` and `raw_reads/<id>_R2.fastq.gz`, in either order. The run starts once both are there, and only once. Only `_R1`/`_R2` mark mates: rename SRA-style `_1`/`_2` files before uploading. The stages detect the layout from these keys.
    *   The trigger Lambda needs `s3:GetObject` on `raw_reads/*` and `s3:ListBucket` to check for the other mate. That IAM change is not applied yet (see [#57](https://github.com/Harry5haw/Geyser-Genomics/issues/57)). Until it is, paired-end uploads fail in the cloud, while single-end works as before.
3.  **Monitor the Execution:** The upload automatically starts the Step Functions state machine. You can follow its progress in the AWS Step Functions console, where each run is named after its sample and upload time, e.g. `SRR062634-20261101T123456Z-1a2b3c4d`. The same name appears in the Batch job names. If S3 delivers the same upload event twice, it still starts only one run. A paired-end run is named after the later of its two uploads.
4.  **Retrieve Results:** When the run succeeds, the outputs are in the same bucket:

| Stage | Output |
| :--- | :--- |
| Decompress | `decompressed/<id>.fastq` (paired-end: `<id>_R1.fastq` and `<id>_R2.fastq`) |
| Quality control | `qc_reports/<id>_fastqc.html` and `.zip` (paired-end: one report per mate, `<id>_R1_fastqc.html` ...) |
| Alignment | `alignments/<id>.bam`, one per sample (coordinate-sorted, duplicates marked with samtools markdup; paired-end runs `samtools fixmate -m` first) |
| Variant calling | `variants/<id>.vcf.gz` |

### 3. Dashboard Management
Each stage records its duration and failures as CloudWatch metrics. A dashboard for them is managed by a Python script instead of Terraform, because the dashboard JSON was hard to manage there. The dashboards are still being finished.

To deploy or update the dashboard, run the following command from the project root:
```bash
python scripts/deploy_dashboard.py
```

---

## 🛣️ Roadmap

| | Milestone | |
| :---: | :--- | :--- |
| ✅ | **Offline pipeline** | End-to-end truth check on every push |
| ✅ | **Cost-safe networking** | NAT gateway optional and off, ECR and log retention limits, budget alert |
| ✅ | **CI hardening** | OIDC with separate dev and prod roles, read-only Terraform roles, prod restricted to `main` |
| ✅ | **DeepVariant on a local GPU** | Measured on a whole chromosome |
| ✅ | **Web UI** | Works offline |
| ✅ | **Pangenome-aware DeepVariant (local)** | GPU, HPRC graph cut to chr20, hap.py benchmark on HG003 chr20 against linear DeepVariant and bcftools; reproduces Google's published result |
| 🎯 | **Pangenome: where it should help most** | Benchmark a sample of under-represented ancestry, align reads to the graph (`vg giraffe`), newer HPRC graph, whole genome. **Top priority** ([#51](../../issues/51)) |
| 🔜 | **First cloud run since the hardening** | One small sample through S3 → Step Functions → Batch, confirming the VCF |
| 🔜 | **Monitoring** | Finish the CloudWatch dashboards for stage durations and failures |
| 🔜 | **DeepVariant in the cloud** | GPU environment in the public subnet, larger instances, stage added to Step Functions |
| 🔜 | **Web UI on S3** | Point the UI at the cloud bucket |
| 🔜 | **Branch protection** | Required review for `prod`, once the repository is public |
