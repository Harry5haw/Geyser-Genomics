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
  access to the raw reads or the alignments;
- with an API base there is an upload page (/uploads/): the browser sends the reads straight to the data lake in
  presigned multipart parts (the web API's /uploads), with the upload token kept in the page's memory only.
"""

import posixpath
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

# The quality gate's threshold profiles an upload may name (S3 metadata gate-profile): gate/thresholds/<name>.toml,
# and the state machine's Check_Gate_Profile accepts exactly these (tests/test_consistency.py-style check in
# web/tests/test_web_upload_page.py). The first is a run's default.
GATE_PROFILES = (
    ("default", "Default: human germline WGS at about 30x (checked on HG003 and HG005)"),
    ("synthetic", "Synthetic: reads from scripts/make_test_data.py"),
    ("smoke", "Smoke: the small cloud checkpoint samples (10,000 reads on chr20)"),
)
# reference/<name> objects that are a FASTA (not their .fai / aligner indexes): the run's reference choices.
FASTA_SUFFIXES = (".fa", ".fasta", ".fna", ".fa.gz", ".fasta.gz", ".fna.gz")


def reference_names(keys) -> list:
    """The FASTA names directly under reference/ (the state machine refuses a name with a '/')."""
    names = (k[len("reference/") :] for k in keys if k.startswith("reference/"))
    return sorted(n for n in names if n and "/" not in n and n.lower().endswith(FASTA_SUFFIXES))


# The web API names each caller's VCF by its own kind (lambda/web_api/index.py): `vcf` is always bcftools'
# variants/<id>.vcf.gz, so a sample whose run record makes DeepVariant's or the pangenome caller's VCF its calls links
# that one by its folder.
API_KIND_BY_FOLDER = {
    ("vcf", "variants_deepvariant"): "deepvariant_vcf",
    ("tbi", "variants_deepvariant"): "deepvariant_tbi",
    ("vcf", "variants_pangenome"): "pangenome_vcf",
    ("tbi", "variants_pangenome"): "pangenome_tbi",
}


@dataclass
class StaticSite:
    api_base: str = ""  # the web API's origin, no trailing slash; "" = a self-contained site (files copied)
    max_pages: int = 20  # call pages published per sample (the first page and up to this many in all)
    samples: dict = field(default_factory=dict)  # sample_id -> SampleView, built from one listing
    variants: dict = field(default_factory=dict)  # VCF key -> VariantTable of its first max_pages pages
    references: list = field(default_factory=list)  # reference/ FASTA names, for the upload page's choice

    def file_href(self, sample_id: str, kind: str, key: str) -> str:
        """Where a public file's link points: the API's presigned redirect, or the copy beside the pages."""
        if self.api_base:
            api_kind = API_KIND_BY_FOLDER.get((kind, key.split("/", 1)[0]), kind)
            return f"{self.api_base}/files/{sample_id}/{api_kind}"
        return f"/samples/{sample_id}/files/{posixpath.basename(key)}"

    def status_url(self, sample_id: str) -> str:
        """The API's run status for a sample, or "" with no API (the static site then doesn't poll)."""
        return f"{self.api_base}/status/{sample_id}" if self.api_base else ""


def static_of(request):
    """The request's StaticSite, or None when the live UI is rendering."""
    return getattr(request, "geyser_static", None)
