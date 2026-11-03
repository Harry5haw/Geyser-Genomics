# Release notes

One page per milestone, from the milestone's merged pull requests. **v0.3, v0.35 and v0.4 are tagged**, with
[GitHub Releases](../../../../releases); v1.0 and v2.0 are drafts. Tagging and publishing a release is the
maintainer's decision. Each page ends with what was (or is still) open at its tag.

| Version | Milestone | State |
| :--- | :--- | :--- |
| [v0.3](v0.3.md) | Observability & Automation | **Tagged** 2 Nov 2026: dashboard, alarm, run names, per-stage cost and resource use |
| [v0.35](v0.35.md) | Refactor Legacy Code | **Tagged** 2 Nov 2026: legacy names gone from the code; the legacy AWS stack teardown (#46) is still open |
| [v0.4](v0.4.md) | Google DeepVariant Integration | **Tagged** 2 Nov 2026: DeepVariant from the local GPU to a cloud GPU (#125), pangenome-aware calling benchmarked |
| [v1.0](v1.0.md) | Public Release | Draft. The repository is public and the pipeline verified on real data; waits for the maintainer's protection settings (#47) |
| [v2.0 "XLR8"](v2.0.md) | Architecture | The speed release: three stages, bwa-mem2, slim image, Spot, sharded calling, quality gate. In progress: the gate's state machine step (#162). Overlaps v1.0's milestone: see its page |

Each tagged release has a banner in [`docs/assets/`](../assets/) (`<name>-<version>-release`), also shown on
its GitHub Release. The banner for a future 3.0, "Project Alpha Genome", is already there:
`alpha-genome-3.0-release.jpg`.

The [changelog](../../changelog.md) lists the same changes by date. These pages group them by version.
Some early pull requests have no milestone; they are listed under the version their content belongs
to, marked *(no milestone)*.
