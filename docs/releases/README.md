# Release notes

One page per milestone, from the milestone's merged pull requests. **v0.3, v0.35, v0.4, v1.0, v1.1 and v1.2 are
tagged**, with [GitHub Releases](../../../../releases); v1.3 and v2.0 are drafts. Tagging and publishing a release is the
maintainer's decision. Each page ends with what was (or is still) open at its tag.

| Version | Milestone | State |
| :--- | :--- | :--- |
| [v0.3](v0.3.md) | Observability & Automation | **Tagged** 2 Nov 2026: dashboard, alarm, run names, per-stage cost and resource use |
| [v0.35](v0.35.md) | Refactor Legacy Code | **Tagged** 2 Nov 2026: legacy names gone from the code; the legacy AWS stack teardown (#46) is still open |
| [v0.4](v0.4.md) | Google DeepVariant Integration | **Tagged** 2 Nov 2026: DeepVariant from the local GPU to a cloud GPU (#125), pangenome-aware calling benchmarked |
| [v1.0](v1.0.md) | Public Release | **Tagged** 4 Nov 2026: the repository public, the pipeline verified on real data, and the protections set up (ruleset, `prod` environment, private vulnerability reporting, #47) |
| [v1.1 "Measured"](v1.1.md) | (checkpoint) | **Tagged** 4 Nov 2026. What was measured since v1.0: the 25-run cost × turnaround grid (#221), the right-sizing saving on a real sample (#222), a real-size GPU DeepVariant run (#223) and DeepVariant batch × shards on the local GPU (#216); one pipeline change, giraffe as a cloud align option (#197, checked on chr20) |
| [v1.2 "Fast"](v1.2.md) | (checkpoint) | **Tagged** 4 Nov 2026: the web UI rebuilt without a framework (every page under 10 KB, LCP under 0.8 s on mobile, Lighthouse 100, #226) and interactive 3D figures (WebGL in a Worker, #227); no pipeline change |
| [v1.3 "Big inputs"](v1.3.md) | v1.3 Big inputs | Draft: whole genomes on the existing Fargate Spot environments. Size routing at 5 GB; pre-flight, 24 chunks aligned 5 at a time and a streaming merge, equivalent to one align job (hap.py F1 within 0.00005 on real chr20); region-streamed calling; per-run parallelism; raw-read tiers (#225, #229). Also the teraflow teardown (#46) and the OIDC provider adopted (#134). **Slots** for the canaries and the HG003 genome run |
| [v2.0 "XLR8"](v2.0.md) | Architecture | Draft, the cumulative speed release: three stages, bwa-mem2, CRAM, slim image, Spot, sharded calling, the quality gate in fail mode; real chr20 in ~22 min for ~$0.07. Per-run gate profiles (#178), right-sizing (#174), the per-upload reference (#183), per-run overrides (#192), alignment expiry (#190), the cost grid, the GPU DeepVariant run and giraffe in the cloud (#197) are in; no slot left. Overlaps v1.0's milestone: see its page |

Each tagged release has a banner in [`docs/assets/`](../assets/) (`<name>-<version>-release`), also shown on
its GitHub Release. v1.0 "The Upgrade Release" uses `upgrade-1.0-release.jpg`. The drafts have theirs ready:
2.0 "XLR8", and a future 3.0 "Project Alpha Genome" (`alpha-genome-3.0-release.jpg`).

The [changelog](../../changelog.md) lists the same changes by date. These pages group them by version.
Some early pull requests have no milestone; they are listed under the version their content belongs
to, marked *(no milestone)*.
