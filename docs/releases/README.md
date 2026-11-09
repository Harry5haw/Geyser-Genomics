# Release notes

One page per milestone, from the milestone's merged pull requests. **v0.3, v0.35, v0.4, v1.0, v1.1 and v1.2 are
tagged**, with [GitHub Releases](../../../../releases); v1.3, v1.5, v2.0, v2.1 and v3.0 are tagged too. Tagging and publishing a release is the
maintainer's decision. Each page ends with what was (or is still) open at its tag.

| Version | Milestone | State |
| :--- | :--- | :--- |
| [v0.3](v0.3.md) | Observability & Automation | **Tagged** 2 Nov 2026: dashboard, alarm, run names, per-stage cost and resource use |
| [v0.35](v0.35.md) | Refactor Legacy Code | **Tagged** 2 Nov 2026: legacy names gone from the code; the legacy AWS stack teardown (#46) is still open |
| [v0.4](v0.4.md) | Google DeepVariant Integration | **Tagged** 2 Nov 2026: DeepVariant from the local GPU to a cloud GPU (#125), pangenome-aware calling benchmarked |
| [v1.0](v1.0.md) | Public Release | **Tagged** 4 Nov 2026: the repository public, the pipeline verified on real data, and the protections set up (ruleset, `prod` environment, private vulnerability reporting, #47) |
| [v1.1 "Measured"](v1.1.md) | (checkpoint) | **Tagged** 4 Nov 2026. What was measured since v1.0: the 25-run cost × turnaround grid (#221), the right-sizing saving on a real sample (#222), a real-size GPU DeepVariant run (#223) and DeepVariant batch × shards on the local GPU (#216); one pipeline change, giraffe as a cloud align option (#197, checked on chr20) |
| [v1.2 "Fast"](v1.2.md) | (checkpoint) | **Tagged** 4 Nov 2026: the web UI rebuilt without a framework (every page under 10 KB, LCP under 0.8 s on mobile, Lighthouse 100, #226) and interactive 3D figures (WebGL in a Worker, #227); no pipeline change |
| [v1.3 "Big inputs"](v1.3.md) | v1.3 Big inputs | **Tagged** 7 Nov 2026. Whole genomes on the existing Fargate Spot environments. Size routing at 5 GB; pre-flight, 24 chunks aligned 5 at a time and a streaming merge, equivalent to one align job (hap.py F1 within 0.00005 on real chr20); region-streamed calling; per-run parallelism; raw-read tiers (#225, #229). Also the teraflow teardown (#46) and the OIDC provider adopted (#134). HG003 30x whole genome: 2 h 59 min, $6.50 at on-demand list prices, gate 11/11, hap.py SNP F1 0.9903 / indel 0.9506 (as on chr20) |
| [v2.0 "XLR8"](v2.0.md) | Architecture | **Tagged** 8 Nov 2026, the cumulative speed release: three stages, bwa-mem2, CRAM, slim image, Spot, sharded calling, the quality gate in fail mode; real chr20 in ~22 min for ~$0.07. Per-run gate profiles (#178), right-sizing (#174), the per-upload reference (#183), per-run overrides (#192), alignment expiry (#190), the cost grid, the GPU DeepVariant run and giraffe in the cloud (#197) are in. Whole genome (HG003 30x): DeepVariant split on the GPU in 72.9 min, ~$4.1, SNP F1 0.9959 / indel 0.9950 vs bcftools 0.9903 / 0.9506 (errors −57% / −90%). DeepVariant on CPU on the same examples: call_variants 66.5 min vs 27.2 on the GPU, $1.30 list; no slot left. Overlaps v1.0's milestone: see its page |
| [v2.1 "Hosted"](v2.1.md) | v2.1 Hosted | **Tagged** 9 Nov 2026. The pipeline on the web: a public read-only site on CloudFront with token-gated uploads, a web API and a Publish step after every run (#213), a strict hash-based CSP, honest run pages (#260), heat-map figures (#248). Observability: metrics per stage (#249), run records with route and caller outcome (#251), dashboard v2 with spend, running now, gate values and the hosted site (#253, #255, #259). Checkpoints CLOUD021 (gate fail, as designed) and CLOUD022 (SUCCEEDED, 5 min 39 s); Lighthouse on the live site, LCP 0.84-0.93 s (#240) |
| [v3.0 "Alpha Genome"](v3.0.md) | v3.0 Alpha Genome | **Tagged** 9 Nov 2026. Opt-in AlphaGenome regulatory-effect scores for the variants of interest (research use only, never fatal): the Annotate step on its own image, Atlas only in the cloud, streamed genome-scale selection, sized by the VCF (#207, #209); the sample-page panel with the terms notice and no downloads (#263); counts-only dashboard signals (#264); the evaluation harness and design note (#208). CLOUD026 SUCCEEDED in 7 min 7 s, 10/10 SNVs scored; three IAM hotfixes on the way (CLOUD023-025) |

Each tagged release has a banner in [`docs/assets/`](../assets/) (`<name>-<version>-release`), also shown on
its GitHub Release. v1.0 "The Upgrade Release" uses `upgrade-1.0-release.jpg`, 2.0 "XLR8"
`xlr8-2.0-release.png` and 3.0 "Project Alpha Genome" `alpha-genome-3.0-release.jpg`.

The [changelog](../../changelog.md) lists the same changes by date. These pages group them by version.
Some early pull requests have no milestone; they are listed under the version their content belongs
to, marked *(no milestone)*.
