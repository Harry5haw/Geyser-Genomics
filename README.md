<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/geyser-erupt-dark.gif">
  <img src="docs/assets/geyser-erupt-light.gif" alt="Geyser Genomics" width="425" height="112">
</picture>

<h2>Upload a genome. Get your variants. In the cloud, for pennies.</h2>

</div>

For eighteen years I lived with an illness nobody could name. Then my mother, my father and I joined NHS England's **100,000 Genomes Project**, run by **Genomics England**, and gave blood so that all three of our genomes could be sequenced. We were told not to expect an answer. A year later one came: among three billion letters of DNA, a single ultra-rare mutation in the **COA3** gene, causing a mitochondrial defect. An answer like that depends on how well a pipeline reads a genome. So I built my own: Geyser Genomics turns a sequencer's raw reads into a list of variants faster, more accurately and for pennies, and tests pangenome-aware methods that see more of human diversity than a single reference genome can ([the full story](docs/story.md)).

<div align="center">

**Built with**

<p>
  <img src="https://img.shields.io/badge/Google%20DeepVariant-GPU-4285F4?style=flat-square&amp;logo=google&amp;logoColor=white" alt="Google DeepVariant (GPU)">
  <img src="https://img.shields.io/badge/Google%20DeepMind-AlphaGenome-4285F4?style=flat-square&amp;logo=deepmind&amp;logoColor=white" alt="Google DeepMind AlphaGenome">
  <img src="https://img.shields.io/badge/GIAB%20%2F%20NIST-HG003%20·%20HG005-2E7D32?style=flat-square&amp;logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB2ZXJzaW9uPSIxLjEiIGlkPSJMYXllcl8xIiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHg9IjAiIHk9IjAiIHZpZXdCb3g9IjAgNSAxMTIgMzEiIHhtbDpzcGFjZT0icHJlc2VydmUiPiA8c3R5bGU%2BIC5zdDB7ZW5hYmxlLWJhY2tncm91bmQ6bmV3fS5zdDF7ZmlsbDojZmZmfS5zdDJ7ZmlsbDpub25lO3N0cm9rZTojZmZmO3N0cm9rZS13aWR0aDoxLjAxOTM7c3Ryb2tlLWxpbmVjYXA6cm91bmQ7c3Ryb2tlLWxpbmVqb2luOnJvdW5kO3N0cm9rZS1taXRlcmxpbWl0OjEwfSA8L3N0eWxlPiA8ZyBjbGFzcz0ic3QwIj4gPC9nPiA8ZyBjbGFzcz0ic3QwIj4gPC9nPiA8ZyBjbGFzcz0ic3QwIj4gPC9nPiAgPHBhdGggY2xhc3M9InN0MSIgZD0iTTUuOSAzNC42SDBWMTEuOXMwLTQuMyA0LjMtNS43YzMuOS0xLjIgNi41IDEuMyA2LjUgMS4zbDE5LjQgMjFjMSAuNiAxLjQgMCAxLjQtLjZWNi4yaDZ2MjIuNmMwIDEuOS0xLjYgNC4zLTMuOSA1LjItMi4zLjktNC44LjktNy44LTEuNkw3LjcgMTIuNmMtLjUtLjUtMS43LS42LTEuOC40djIxLjZ6Ii8%2BIDxwYXRoIGNsYXNzPSJzdDEiIGQ9Ik00MS40IDYuMmg1Ljl2MjAuNGMwIDIuMiAxLjggMi4yIDMuNiAyLjJoMjYuNGMxLjEgMCAyLjMtMS4zIDIuMy0yLjYgMC0xLjQtMS4zLTIuNy0yLjUtMi43SDYwYy0yLjkuMS05LjEtMi42LTkuMS04LjQgMC03LjEgNS44LTguNiA4LjUtOC44SDEwOHY2SDk1Ljh2MjIuNWgtNS45VjEyLjJINjAuMmMtMi44LS4yLTQuOSA0LjYtLjIgNS4zaDE4LjNjMi44IDAgNy4zIDIuNCA3LjMgOC4zIDAgNi0zLjUgOC44LTcuMyA4LjhoLTI3Yy00LjUgMC02LjEtMS03LjYtMi41LTEuNS0xLjQtMS43LTIuMi0yLjItNS4ybC0uMS0yMC43eiIvPiA8L3N2Zz4g" alt="GIAB / NIST reference genomes HG003 and HG005">
  <img src="https://img.shields.io/badge/Human%20Pangenome%20Reference%20Consortium-vg%20giraffe-2E7D32?style=flat-square&amp;logo=data%3Aimage%2Fpng%3Bbase64%2CiVBORw0KGgoAAAANSUhEUgAAACAAAAAbCAMAAAAqGX2oAAAAflBMVEUbGyNbYxhVHAsfJ1EBAQBtigCgkFA5SAQXSmCGeEMOEQsWcImEQhRHKXSWuwDVwGtJIIl%2Ff3%2BbwQAfHBCONgACAQEAAAARFwoAAQAlJwcvOAUdJABSZwFjegAAAABNRicAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADdd4HXAAAAIHRSTlP9%2Fv%2F%2BYP7%2B%2F%2F7%2FKP7%2F%2F%2F%2F%2F%2FwL%2FqP%2F%2BAf7U%2Fv7%2B%2F%2F%2Br%2FwBDyGcAAAGcSURBVHjadZOJktswCIYBSZaPZNvuliB02O%2F%2FlkXxkbQ7xZ6xRnz85pDgcdns18RmafXzaxfOhU%2FjyLuBQf4fwFtsB7KY3z11%2FDvwtTHLaJGbmB92ofUC5iTIBAtUUYab%2BQWg%2F%2BcEEusGII61Fr6RSSzL0mXSDqxiABPZTs72sRxAbLn%2FBR6ec%2BRIBOxGzGg1jJapROl5eAOScMyVODgntSpRBrqp7ImmB3i3uE40NYkagBgtD0LaEQ%2BrjMsiUj8Y7k6Dau15sGzbXiv07jps0YKZQ6vhE61EoKOrqVdjdTlpDWvLljzGRkQn0PvuRoCf%2BJsxBOaI0cXLewDMdwdUq2AT07dWO%2FcCnjncmTWICqJaE2wod75yWHmfX8GipUYeT%2BFjYuDPZdBSQtnQHkI5d7118ljmkBvyMEw%2FhmE4FdJzFofhRzOMNto2VFbur39Oc%2Fe3VmqQYcABceqfYdD1PA89HkVLDBtOE06GGDTB%2FGs%2FckZIk1KKmPbL0nydyZUllG7tDVj%2FPtUYVTW3V7j%2Fdi%2F4Xd1%2Fuzj%2Fu1l%2FADQEO10XycpvAAAAAElFTkSuQmCC" alt="Human Pangenome Reference Consortium, vg giraffe">
  <br/>
  <img src="https://img.shields.io/badge/AWS-Step%20Functions%20·%20Batch%20·%20Fargate%20Spot%20·%20GPU%20·%20CloudFront-FF9900?style=flat-square&amp;logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyBmaWxsPSIjZmZmIiB2aWV3Qm94PSIwIDAgMjQgMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI%2BPHBhdGggZD0iTTYuNzYgMTAuMDRjMCAuMy4wMy41NC4wOS43MS4wNi4xOC4xNC4zNy4yNi41OC4wNC4wNi4wNi4xMy4wNi4xOCAwIC4wOC0uMDUuMTYtLjE1LjI0bC0uNS4zNGEuMzguMzggMCAwIDEtLjIxLjA3Yy0uMDggMC0uMTYtLjA0LS4yNC0uMTFhMi40NyAyLjQ3IDAgMCAxLS4yOS0uMzggNi4xOCA2LjE4IDAgMCAxLS4yNS0uNDdjLS42Mi43My0xLjQxIDEuMS0yLjM1IDEuMS0uNjcgMC0xLjIxLS4xOS0xLjYtLjU3LS4zOS0uMzgtLjU5LS44OS0uNTktMS41MyAwLS42OC4yNC0xLjIzLjczLTEuNjQuNDktLjQxIDEuMTMtLjYyIDEuOTYtLjYyLjI3IDAgLjU1LjAyLjg1LjA2LjMuMDQuNi4xLjkyLjE4di0uNThjMC0uNjEtLjEzLTEuMDMtLjM4LTEuMjgtLjI2LS4yNS0uNjktLjM3LTEuMy0uMzctLjI4IDAtLjU3LjAzLS44Ni4xLS4yOS4wNy0uNTguMTYtLjg2LjI3YTIuMjkgMi4yOSAwIDAgMS0uMjguMS40OS40OSAwIDAgMS0uMTMuMDJjLS4xMSAwLS4xNy0uMDgtLjE3LS4yNXYtLjM5YzAtLjEzLjAyLS4yMi4wNi0uMjhhLjYuNiAwIDAgMSAuMjItLjE3Yy4yOC0uMTQuNjEtLjI2IDEtLjM2YTQuODQgNC44NCAwIDAgMSAxLjI1LS4xNWMuOTUgMCAxLjY0LjIyIDIuMDkuNjUuNDQuNDMuNjYgMS4wOC42NiAxLjk2djIuNTl6bS0zLjI0IDEuMjFjLjI2IDAgLjUzLS4wNS44Mi0uMTQuMjktLjEuNTQtLjI3Ljc2LS41MS4xMy0uMTUuMjItLjMyLjI3LS41MS4wNS0uMTkuMDgtLjQyLjA4LS42OXYtLjM0YTYuNjYgNi42NiAwIDAgMC0uNzMtLjE0IDYuMDIgNi4wMiAwIDAgMC0uNzUtLjA1Yy0uNTQgMC0uOTMuMS0xLjE5LjMyLS4yNi4yMS0uMzkuNTItLjM5LjkyIDAgLjM4LjEuNjYuMjkuODUuMTkuMi40Ny4zLjg0LjN6bTYuNDEuODZjLS4xNCAwLS4yNC0uMDItLjMtLjA4LS4wNi0uMDUtLjEyLS4xNi0uMTctLjMxTDcuNTkgNS41NWExLjQgMS40IDAgMCAxLS4wNy0uMzJjMC0uMTMuMDYtLjIuMTktLjJoLjc4Yy4xNSAwIC4yNi4wMy4zMS4wOC4wNy4wNS4xMS4xNi4xNi4zMWwxLjM0IDUuMjggMS4yNS01LjI4Yy4wNC0uMTYuMDktLjI2LjE1LS4zMWEuNTUuNTUgMCAwIDEgLjMyLS4wOGguNjRjLjE1IDAgLjI2LjAzLjMyLjA4LjA2LjA1LjEyLjE2LjE1LjMxbDEuMjYgNS4zNSAxLjM4LTUuMzVjLjA1LS4xNi4xLS4yNi4xNi0uMzFhLjUyLjUyIDAgMCAxIC4zMS0uMDhoLjc0Yy4xMyAwIC4yLjA3LjIuMiAwIC4wNC0uMDEuMDgtLjAyLjEzYTEuMTQgMS4xNCAwIDAgMS0uMDYuMmwtMS45MiA2LjE3Yy0uMDUuMTYtLjEuMjYtLjE3LjMxYS41MS41MSAwIDAgMS0uMy4wOGgtLjY5Yy0uMTUgMC0uMjYtLjAyLS4zMi0uMDgtLjA2LS4wNi0uMTItLjE2LS4xNS0uMzJsLTEuMjQtNS4xNS0xLjIzIDUuMTRjLS4wNC4xNi0uMDkuMjYtLjE1LjMyLS4wNy4wNi0uMTguMDgtLjMyLjA4em0xMC4yNi4yMWMtLjQxIDAtLjgzLS4wNS0xLjIzLS4xNC0uNC0uMS0uNzEtLjItLjkyLS4zMi0uMTMtLjA3LS4yMS0uMTUtLjI1LS4yMmEuNTYuNTYgMCAwIDEtLjA1LS4yMnYtLjQxYzAtLjE3LjA2LS4yNS4xOC0uMjUuMDUgMCAuMS4wMS4xNC4wMi4wNS4wMi4xMi4wNS4yLjA4LjI3LjEyLjU3LjIxLjg4LjI4LjMyLjA2LjYzLjEuOTUuMS41IDAgLjg5LS4wOSAxLjE3LS4yNmEuODYuODYgMCAwIDAgLjQxLS43Ni43OC43OCAwIDAgMC0uMjEtLjU2Yy0uMTQtLjE1LS40Mi0uMjktLjgxLS40MWwtMS4xNi0uMzZjLS41OC0uMTgtMS4wMS0uNDUtMS4yOC0uODFhMS45IDEuOSAwIDAgMS0uNC0xLjE2YzAtLjM0LjA3LS42My4yMi0uODkuMTQtLjI2LjM0LS40OC41Ny0uNjUuMjQtLjE4LjUxLS4zMi44My0uNDEuMzItLjEuNjYtLjE0IDEuMDEtLjE0LjE3IDAgLjM2LjAxLjU0LjAzLjE4LjAyLjM1LjA2LjUyLjA5LjE2LjA0LjMxLjA4LjQ2LjEzLjE0LjA1LjI2LjEuMzQuMTRhLjY5LjY5IDAgMCAxIC4yNC4yLjQzLjQzIDAgMCAxIC4wNy4yNnYuMzhjMCAuMTctLjA2LjI2LS4xOC4yNmEuODMuODMgMCAwIDEtLjMtLjEgMy42NSAzLjY1IDAgMCAwLTEuNTMtLjMxYy0uNDYgMC0uODEuMDctMS4wNi4yMi0uMjUuMTUtLjM4LjM4LS4zOC43MSAwIC4yMi4wOC40Mi4yNC41Ny4xNi4xNS40NS4zLjg4LjQ0bDEuMTMuMzZjLjU3LjE4Ljk5LjQ0IDEuMjQuNzcuMjUuMzMuMzcuNy4zNyAxLjEyIDAgLjM0LS4wNy42Ni0uMjEuOTMtLjE0LjI3LS4zNC41MS0uNTguNy0uMjUuMi0uNTQuMzQtLjg5LjQ1LS4zNi4xMS0uNzMuMTctMS4xNC4xN3pNMjEuNyAxNi4yMWMtMi42MyAxLjk0LTYuNDQgMi45Ny05LjcyIDIuOTctNC42IDAtOC43NC0xLjctMTEuODctNC41My0uMjUtLjIyLS4wMi0uNTMuMjctLjM1IDMuMzggMS45NiA3LjU2IDMuMTUgMTEuODggMy4xNSAyLjkxIDAgNi4xMS0uNjEgOS4wNi0xLjg1LjQ0LS4yLjgxLjI5LjM4LjYxek0yMi43OSAxNC45NmMtLjM0LS40My0yLjIyLS4yMS0zLjA3LS4xLS4yNi4wMy0uMjktLjE5LS4wNi0uMzYgMS41LTEuMDUgMy45Ny0uNzUgNC4yNS0uNC4yOS4zNi0uMDggMi44My0xLjQ5IDQuMDEtLjIxLjE4LS40Mi4wOS0uMzMtLjE1LjMyLS43OSAxLjAzLTIuNTcuNjktMi45OXoiLz48L3N2Zz4%3D" alt="AWS: Step Functions, Batch, Fargate Spot, GPU, CloudFront">
  <img src="https://img.shields.io/badge/Terraform-IaC-7B42BC?style=flat-square&amp;logo=terraform&amp;logoColor=white" alt="Terraform">
  <img src="https://img.shields.io/badge/Rust-quality%20gate-B7410E?style=flat-square&amp;logo=rust&amp;logoColor=white" alt="Rust">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Docker-images-2496ED?style=flat-square&amp;logo=docker&amp;logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/GitHub%20Actions-OIDC-2088FF?style=flat-square&amp;logo=githubactions&amp;logoColor=white" alt="GitHub Actions with OIDC">
</p>

<sub>Google DeepVariant (GPU) · Google DeepMind AlphaGenome · GIAB / NIST reference genomes (HG003, HG005) · Human Pangenome Reference Consortium graphs with vg giraffe · AWS (Step Functions, Batch, Fargate Spot, GPU, CloudFront) · Terraform · Rust · Python · Docker · GitHub Actions with OIDC.<br/>These are the tools and data this project uses. It is not affiliated with or endorsed by any of their owners.</sub>

<table>
  <tr>
    <td align="center" width="20%"><h2>~3 h</h2>a whole human genome,<br/>reads to variants</td>
    <td align="center" width="20%"><h2>~$0.07</h2>one chromosome,<br/>in about 22 minutes</td>
    <td align="center" width="20%"><h2>−57% / −90%</h2>SNP / indel errors<br/>with DeepVariant</td>
    <td align="center" width="20%"><h2>2.4×</h2>faster DeepVariant<br/>calling on a GPU</td>
    <td align="center" width="20%"><h2>0</h2>stored AWS keys<br/>(CI signs in with OIDC)</td>
  </tr>
</table>

<sub>Measured on the GIAB reference genomes: HG003 whole genome at 30x in 2 h 59 min; HG003/HG005 chr20 in 21-23 min for $0.071-0.079 at on-demand list prices; DeepVariant against bcftools on the HG003 whole genome, scored with hap.py (SNP F1 0.9959, indel F1 0.9950); DeepVariant's <code>call_variants</code> on the same whole-genome examples, 27.2 min on the GPU against 66.5 min on CPU. <a href="docs/releases/README.md">The release notes</a> have every run.</sub>

<p>
  <a href="https://d1bp7nfasxklsf.cloudfront.net"><img src="https://img.shields.io/badge/See%20it%20live-the%20results%20site-1A73E8?style=for-the-badge" alt="See it live: the results site"></a>
  <a href="https://d1bp7nfasxklsf.cloudfront.net/figures/"><img src="https://img.shields.io/badge/Results%20in%203D-interactive%20figures-1A73E8?style=for-the-badge" alt="Results in 3D: interactive figures"></a>
  <a href="https://github.com/Harry5haw/Geyser-Genomics/releases"><img src="https://img.shields.io/badge/Releases-v0.3%20to%20v3.0-1A73E8?style=for-the-badge" alt="Releases"></a>
</p>

<p><a href="https://github.com/Harry5haw/Geyser-Genomics/actions/workflows/web-perf.yml"><img src="https://img.shields.io/github/actions/workflow/status/Harry5haw/Geyser-Genomics/web-perf.yml?branch=main&style=flat-square&label=Web%20performance%20budgets" alt="Web performance budgets (Lighthouse CI on main)"></a></p>

</div>

## What I built

**Python on AWS, end to end**
- **Python backend and data processing:** about 34,000 lines of Python. A CLI stage dispatcher, storage and metrics layers with S3/CloudWatch and local back ends behind one interface, two Lambdas (the S3 trigger, a web API with multipart presigned uploads), a provenance record per run, a Django app and the ops tooling.
- **Event-driven services on AWS:** an S3 upload fires a Lambda, which starts Step Functions. Each stage then runs as its own container on AWS Batch (Fargate Spot first, on-demand fallback, an EC2 GPU environment for DeepVariant). Five images, one per kind of job; stages talk only through S3 key conventions.
- **Resilience:** infrastructure failures (lost Spot hosts, failed task starts) are retried; tool failures and corrupt uploads fail at once, with a notification. Every stage and run has a time limit, and a pre-flight pass rejects a bad input in minutes, before any alignment. The 30x whole-genome run took 20 Spot interruptions, and every one was retried and recovered.
- **Performance, measured:** a 25-run cost × turnaround grid chose the job sizes. bwa-mem2 made alignment 1.56× faster, a slim image cut job start-up waits by 26%, and DeepVariant on a GPU calls 2.4× faster than on CPU.

**Distributed data processing at scale**
- **A whole human genome in 2 h 59 min:** 718 million reads streamed through a pre-flight check, dealt into 30 chunks aligned 15 at a time, merged in place over presigned HTTPS (no local copy), then called in 244 region shards, 48 at a time. On real chr20, the chunked route scores within 0.00005 F1 of a single alignment job.
- **Scatter-gather orchestration:** Step Functions `Parallel` and `Map` states, a size-based route (under or over 5 GB), and per-run overrides validated before any job starts. Calling shards read only their region of a 16 GB CRAM, with HTTP range reads checked against the ETag.

**Testing, infrastructure as code and delivery**
- **Test automation:** about 1,400 unit tests (about 40% of the code is tests), about 250 tests that run the deployed state machine in Step Functions Local, an offline S3 end-to-end suite on moto, and four Docker end-to-end suites. The pipeline's truth check builds a genome with planted mutations and fails unless exactly those are found.
- **Terraform:** about 3,600 lines over three state files (dev, web, account), with S3 state and locking. Every change is a reviewed plan, with the expected add/change/destroy counts written down before planning.
- **CI/CD and code review:** GitHub Actions with OIDC (no stored AWS keys, read-only CI roles). Every change goes through a pull request (170 merged) behind a protected `main` with 7 required checks and a code review. Work was tracked as issues, milestones and releases on a project board.
- **Cost guardrails:** a budget alert and a project budget whose kill switch attaches a deny-all policy by itself. A real chromosome costs about $0.07; between runs only storage costs anything.
- **Front end and observability:** a static results site on CloudFront (JavaScript: WebGL 3D figures in a Worker, multipart uploads up to 256 GiB). It has four themes including WCAG AAA high contrast and Lighthouse budgets in CI. A CloudWatch dashboard tracks runs, spend, stage durations and quality-gate results.

**The genomics**
- **Accuracy against the GIAB truth sets:** GPU DeepVariant cut SNP errors by 57% and indel errors by 90% against bcftools on the HG003 whole genome. Graph alignment (vg giraffe, Human Pangenome Reference Consortium graphs) halved DeepVariant's errors against BWA on chr20 (−51%).
- **A quality gate in Rust** fails a run that succeeded but isn't good enough, with named reasons, against thresholds calibrated on real genomes. **AlphaGenome annotation** scores the variants of interest, with an evaluation [pre-registered](docs/prereg/rp1-alphagenome.md) before any results.

**How it was built:** the code was written with Claude Code (Anthropic's AI coding agent); I designed the system and directed the work. The decisions and reviews were mine, and infrastructure changes ran under rules I set: Claude's applies needed a reviewed plan or a narrow standing approval, and account-level, compute and CDN changes were mine. The agents worked under a [bounded AWS role with its own kill switch](docs/design/claude-guardrails.md), and an AI-assistance log is kept with the project's write-up.

## What's new

- **v3.1 "Redesign"** (merged, not yet tagged): the web UI and hosted site in a Google-style look: four themes, the erupting logo, one progress helix across the stages, DeepVariant and AlphaGenome motion, rainbow figures with a 2D/3D toggle, and 256 GiB uploads.
- **[v3.0 "Alpha Genome"](docs/releases/v3.0.md)** (9 Nov 2026): AlphaGenome regulatory-effect scores for the variants of interest.
- **[v2.1 "Hosted"](docs/releases/v2.1.md)** (9 Nov 2026): the public results site on CloudFront, the web API and a Publish step after every run.
- **[v2.0 "XLR8"](docs/releases/v2.0.md)** (8 Nov 2026): whole genomes, GPU DeepVariant, and a real chromosome in ~22 minutes for ~$0.07.

In numbers, at the v3.0 tag: about 42,600 lines of code (Python, Terraform, Rust, JavaScript), 170 merged pull requests, 11 GitHub Releases, and 39 recorded cloud runs, from synthetic checkpoints to a 30x whole genome.

## Try it in 2 minutes, no AWS account

```bash
docker build -f Dockerfile -t geyser-app .            # every pipeline tool, pinned
python scripts/make_test_data.py                      # a small genome with 10 planted mutations
python scripts/run_local.py --sample SYNTH001 --reference synthetic_ref.fa --check   # all stages in Docker; finds all 10
```

Needs Docker and Python 3.11. More options (paired-end, sharded calling, DeepVariant on a local GPU, the tests) are in [docs/local-development.md](docs/local-development.md).

## Learn more

| | |
| :--- | :--- |
| [The full story](docs/story.md) | The Odyssey: 18 years to a diagnosis, and the maths of a 1 in 256 million chance |
| [Architecture and features](docs/architecture.md) | How it works, the diagrams, what each technology does, design goals, the repository layout |
| [Results and benchmarks](docs/results.md) | Accuracy (hap.py against GIAB), pangenome and graph alignment, cost and turnaround, GPU timing |
| [Run it locally](docs/local-development.md) | The offline pipeline, tests, the quality gate, DeepVariant on a local GPU |
| [Deployment](docs/deployment.md) · [Operations runbook](docs/operations.md) | Deploying with Terraform, starting and watching runs, monitoring, rollback |
| [Web UI](docs/web-ui.md) | The Django app and the published static site |
| [Release notes](docs/releases/README.md) · [Roadmap](docs/roadmap.md) · [Changelog](changelog.md) | What changed, release by release |
| [Design notes](docs/design/) · [Benchmark history](docs/benchmarks/README.md) · [Figures](docs/figures/README.md) | Why not Nextflow, guardrails, cloud DeepVariant, every hap.py result, how each figure is made |

## 🧬 Built on the shoulders of giants

Geyser is the plumbing: it runs proven open tools at cloud scale, checks their results and shows them. The science comes from these projects.

<table>
  <tr>
    <td width="50%" valign="top">
      <h3>DeepVariant (Google)</h3>
      <img src="docs/assets/third-party/deepvariant/how-deepvariant-works.png" alt="DeepVariant's two steps: align the sequencer's reads to the reference genome, then infer the true sequence where the reads differ from it" width="100%"/>
      <img src="docs/assets/third-party/deepvariant/pileup-channels.png" alt="The six channels DeepVariant sees for one candidate variant: read base, base quality, mapping quality, strand, read supports variant, base differs from reference" width="100%"/>
      <p>A neural network that looks at the reads around each candidate variant as an image and decides the genotype. In Geyser it runs on a cloud GPU and cuts errors against the classic caller by more than half.<br/>
      <sub>Images from the <a href="https://google.github.io/deepvariant/">DeepVariant blog</a>, © Google, BSD-3-Clause (<a href="docs/assets/third-party/deepvariant/NOTICE.md">notice</a>). Not affiliated with or endorsed by Google.</sub></p>
    </td>
    <td width="50%" valign="top">
      <h3>AlphaGenome (Google DeepMind)</h3>
      <img src="docs/assets/alphagenome-panel-cloud026.png" alt="Geyser's own AlphaGenome panel for cloud run CLOUD026: 10 variants scored, the research-only notice and the AlphaGenome output terms notice; the scores themselves are blurred here" width="100%"/>
      <p>An AI model that predicts how a DNA change may affect gene regulation. Geyser scores a run's variants of interest with it and shows them on the run's page, for research use only.<br/>
      <sub>Our own screenshot (CLOUD026), scores blurred: the live site shows them with the <a href="https://deepmind.google.com/science/alphagenome/output-terms">AlphaGenome output terms</a>. Read DeepMind's post on the <a href="https://deepmind.google/blog/alphagenome-atlas-a-predictive-map-of-every-possible-dna-letter-change-in-the-human-genome/">AlphaGenome Atlas</a>.</sub></p>
    </td>
  </tr>
</table>

Also: [fastp](https://github.com/OpenGene/fastp) (quality control), [bwa-mem2](https://github.com/bwa-mem2/bwa-mem2) (alignment), [samtools and bcftools](https://www.htslib.org/) (files and variant calling), [vg giraffe](https://github.com/vgteam/vg) (pangenome alignment) and the [GIAB](https://www.nist.gov/programs-projects/genome-bottle) truth sets that every number above is scored against.

## 🤝 Contributing, Security and Citing

- [CONTRIBUTING.md](CONTRIBUTING.md): set-up, the checks every change passes, branch/commit/PR conventions, and how infrastructure changes are reviewed.
- [SECURITY.md](SECURITY.md): how to report a vulnerability privately, and what is in scope.
- [CITATION.cff](CITATION.cff): how to cite Geyser and the tools and data it builds on (GitHub's "Cite this repository" uses it).
