"""Static publishing mode (web plan W2): what changes when a page is rendered for the static site.

The publisher (samples/publish.py, `manage.py publish`) renders the same views and templates as the live UI,
with a StaticSite on the request (`request.geyser_static`). Then:
- there are no forms (no upload or demo: the showcase is read-only; uploads have their own token-gated page);
- downloads are only the public allowlist below, linked through the web API (`/files/<id>/<kind>`, a presigned
  redirect) when an API base is configured, else copied next to the pages;
- a run still in progress has no page yet: its row on the list says so and, with an API base, polls the API's
  `/status/<id>` and reloads the list when the run ends (the run's last step publishes its page);
- the run log isn't shown (it can carry paths and commands);
- the sample list and pages come from one storage listing and a preloaded VCF, so publishing needs no read
  access to the raw reads or the alignments.
"""

from dataclasses import dataclass, field

# The public showcase's downloads: the calls, the QC report and data, the gate verdict, the stats and the run
# record (the user's decision, 4 Nov 2026; the web API's PUBLIC_FILES in lambda/web_api/index.py says the same).
# Never the raw reads or the alignments (those carry every read).
PUBLIC_KINDS = (
    "vcf",
    "tbi",
    "csq_vcf",
    "csq_tbi",
    "qc_html",
    "qc_json",
    "gate_verdict",
    "flagstat",
    "mosdepth_summary",
    "mosdepth_dist",
    "bcftools_stats",
    "run_record",
)


@dataclass
class StaticSite:
    api_base: str = ""  # the web API's origin, no trailing slash; "" = a self-contained site (files copied)
    max_pages: int = 20  # call pages published per sample (the first page and up to this many in all)
    samples: dict = field(default_factory=dict)  # sample_id -> SampleView, built from one listing
    variants: dict = field(default_factory=dict)  # VCF key -> VariantTable of its first max_pages pages

    def file_href(self, sample_id: str, kind: str, filename: str) -> str:
        """Where a public file's link points: the API's presigned redirect, or the copy beside the pages."""
        if self.api_base:
            return f"{self.api_base}/files/{sample_id}/{kind}"
        return f"/samples/{sample_id}/files/{filename}"

    def status_url(self, sample_id: str) -> str:
        """The API's run status for a sample, or "" with no API (the static site then doesn't poll)."""
        return f"{self.api_base}/status/{sample_id}" if self.api_base else ""


def static_of(request):
    """The request's StaticSite, or None when the live UI is rendering."""
    return getattr(request, "geyser_static", None)
