"""AlphaGenome's variants of interest on the sample page (3.0, P4): the reader and the panel."""

import json
import os

import pytest

from samples import publish as publish_mod
from samples.alphagenome import TERMS_URL, annotation_key, parse_tsv, read_annotation, sort_rows
from samples.views import _download_kinds

from .web_helpers import FASTP_SINGLE, fastq_gz, put, vcf_gz

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "alphagenome.tsv")
TSV_KEY = "stats/S1/alphagenome.tsv"
RESEARCH_NOTE = "Research use only, not for clinical use."


def fixture_text() -> str:
    with open(FIXTURE, encoding="utf-8") as fh:
        return fh.read()


def record(annotate=None, **extra):
    """An assembled run record; annotate: the Annotate stage's part, or None for a run that didn't annotate."""
    stages = {"CallVariants": {"status": "Success", "started": "2026-11-04T10:00:00Z"}}
    if annotate is not None:
        stages["Annotate"] = {"started": "2026-11-04T10:05:00Z", **annotate}
    return {"schema": 1, "sample_id": "S1", "run_name": "S1-run", "status": "succeeded", "stages": stages, **extra}


def sample_with(storage, tsv=None, run_record=None):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    put(storage, "alignments/S1.cram", b"x")
    put(storage, "variants/S1.vcf.gz", vcf_gz([(100, "A", "G"), (250, "C", "T")]))
    if tsv is not None:
        put(storage, TSV_KEY, tsv)
    if run_record is not None:
        put(storage, "runs/S1.provenance.json", json.dumps(run_record))


# ------------------------------------------------------------------ the reader


def test_the_table_lives_under_stats():
    assert annotation_key("S1") == TSV_KEY


def test_the_fixture_parses_every_row():
    # The fixture is #207's TSV_COLUMNS, in rank order, after AlphaGenome's terms comment line.
    rows, problem = parse_tsv(fixture_text())
    assert problem == "" and len(rows) == 5
    liver = rows[1]
    assert (liver.chrom, liver.pos, liver.ref, liver.alt, liver.gene) == ("synth1", 100, "A", "G", "SYNTHG1")
    assert (liver.avi, liver.top_track, liver.consequence, liver.source) == (
        22.1,
        "RNA_SEQ:liver",
        "5_prime_utr SYNTHG1",
        "atlas",
    )
    assert liver.rna_lfc == -0.62  # signed: ALT lowers expression
    unscored = rows[4]
    assert unscored.avi is None and unscored.gene == "" and unscored.consequence == "intron"
    assert unscored.rna_lfc is None and rows[2].rna_lfc is None  # API rows: no Atlas RNA LFC


def test_comment_lines_before_the_header_are_skipped():
    text = "# terms\n# another note\nchrom\tpos\tref\talt\tavi\nsynth1\t1\tA\tG\t5\n"
    rows, problem = parse_tsv(text)
    assert problem == "" and [(r.pos, r.avi) for r in rows] == [(1, 5.0)]


def test_the_contracts_extra_columns_are_ignored():
    # rank, kind, qual and avi_raw (#207) aren't shown; reading by name means their order doesn't matter.
    rows, problem = parse_tsv("avi_raw\tavi\talt\tref\tpos\tchrom\trank\n0.9\t30\tG\tA\t7\tsynth1\t1\n")
    assert problem == "" and (rows[0].chrom, rows[0].pos, rows[0].avi) == ("synth1", 7, 30.0)


def test_rna_lfc_is_optional():
    rows, problem = parse_tsv("chrom\tpos\tref\talt\tavi\nsynth1\t1\tA\tG\t0.5\n")
    assert problem == "" and rows[0].rna_lfc is None


def test_sorted_by_avi_highest_first_with_unscored_rows_last():
    rows, _ = parse_tsv(fixture_text())
    assert [r.pos for r in sort_rows(rows)] == [250, 100, 400, 700, 520]
    assert [r.pos for r in sort_rows(rows, "asc")] == [700, 400, 100, 250, 520]  # unscored still last


def test_a_table_without_a_required_column_is_unreadable():
    rows, problem = parse_tsv("chrom\tpos\tref\talt\tgene\nsynth1\t1\tA\tG\tX\n")
    assert rows == [] and problem == "columns missing: avi"


def test_a_bad_position_names_its_line():
    rows, problem = parse_tsv("chrom\tpos\tref\talt\tavi\nsynth1\t1\tA\tG\t0.1\nsynth1\tten\tC\tT\t0.2\n")
    assert rows == [] and problem == "line 3: position 'ten' is not a number"


@pytest.mark.parametrize("value", ["", "NA", "nan", "inf", "high"])
def test_an_unusable_avi_is_unscored_not_an_error(value):
    rows, problem = parse_tsv(f"chrom\tpos\tref\talt\tavi\nsynth1\t1\tA\tG\t{value}\n")
    assert problem == "" and rows[0].avi is None


def test_rows_from_the_avi_file_have_no_gene_or_track():
    # The downloadable AVI table holds the score only: the columns may be absent entirely.
    rows, problem = parse_tsv("chrom\tpos\tref\talt\tavi\tsource\nsynth1\t1\tA\tG\t23.5\tavi-file\n")
    assert problem == "" and (rows[0].gene, rows[0].top_track, rows[0].source) == ("", "", "avi-file")


def test_unknown_columns_are_ignored():
    rows, problem = parse_tsv("chrom\tpos\tref\talt\tavi\tmodel_version\nsynth1\t1\tA\tG\t0.5\tv1\n")
    assert problem == "" and rows[0].avi == 0.5


def test_no_table_and_no_annotate_stage_is_not_annotated(storage):
    assert read_annotation(storage, "S1").state == "not_annotated"
    assert read_annotation(storage, "S1", record()).state == "not_annotated"


def test_rows_with_their_sources(storage):
    put(storage, TSV_KEY, fixture_text())
    view = read_annotation(storage, "S1", record({"status": "Success"}))
    assert view.state == "rows" and view.total == 5 and not view.truncated
    assert [r.pos for r in view.rows] == [250, 100, 400, 700, 520]
    assert view.sources == [("atlas", 3), ("api", 2)] and not view.test_data


def test_the_row_limit_keeps_the_highest_scores(storage):
    put(storage, TSV_KEY, fixture_text())
    view = read_annotation(storage, "S1", limit=2)
    assert [r.pos for r in view.rows] == [250, 100] and view.total == 5 and view.truncated


def test_an_unknown_sort_falls_back_to_highest_first(storage):
    put(storage, TSV_KEY, fixture_text())
    assert read_annotation(storage, "S1", order="sideways").sort == "desc"


def test_a_header_only_table_is_annotated_with_nothing_of_interest(storage):
    put(storage, TSV_KEY, "\n".join(fixture_text().splitlines()[:2]) + "\n")  # the terms line and the header
    assert read_annotation(storage, "S1", record({"status": "Success"})).state == "empty"


def test_a_failed_stage_shows_its_error(storage):
    view = read_annotation(storage, "S1", record({"status": "Failure", "error": "API quota exceeded"}))
    assert (view.state, view.error) == ("failed", "API quota exceeded")


def test_a_table_from_an_earlier_run_is_not_this_runs(storage):
    # The latest run didn't annotate (its record has no Annotate stage): the old table stays on disk.
    put(storage, TSV_KEY, fixture_text())
    assert read_annotation(storage, "S1", record()).state == "not_annotated"


def test_without_a_record_the_table_is_trusted(storage):
    put(storage, TSV_KEY, fixture_text())
    assert read_annotation(storage, "S1", None).state == "rows"


def test_a_succeeded_stage_without_its_table_is_unreadable(storage):
    view = read_annotation(storage, "S1", record({"status": "Success"}))
    assert view.state == "unreadable" and view.error == "the table is missing"


def test_fake_scores_are_flagged_as_test_data(storage):
    put(storage, TSV_KEY, "chrom\tpos\tref\talt\tavi\tsource\nsynth1\t1\tA\tG\t0.5\tfake\n")
    assert read_annotation(storage, "S1").test_data


def test_a_storage_error_never_fails_the_page(storage, monkeypatch):
    put(storage, TSV_KEY, fixture_text())

    def denied(key):
        raise PermissionError("AccessDenied")

    monkeypatch.setattr(storage, "open_read", denied)
    view = read_annotation(storage, "S1")
    assert view.state == "unreadable" and "AccessDenied" in view.error


# ------------------------------------------------------------------ the sample page


def test_page_shows_the_table_sorted_by_avi_with_the_research_note(client, storage):
    sample_with(storage, fixture_text(), record({"status": "Success"}))
    body = client.get("/samples/S1/").content.decode()
    assert "Variants of interest" in body and RESEARCH_NOTE in body
    assert body.index("SPLICE_SITES:brain") < body.index("RNA_SEQ:liver") < body.index("CAGE:hepg2")
    assert "31.4" in body and "splice_region SYNTHG1" in body and "5 scored" in body
    # AlphaGenome's output terms: required wherever its outputs are published, with the link.
    assert "By using this information, you agree to AlphaGenome Output Terms of Use found at" in body
    assert f'<a href="{TERMS_URL}"' in body
    assert 'href="?avi=asc#alphagenome"' in body and 'aria-sort="descending"' in body
    assert "RNA LFC" in body and "1.375" in body and "-0.62" in body  # signed (ALT vs REF); a dash when absent


def test_page_sorts_lowest_first_on_request(client, storage):
    sample_with(storage, fixture_text(), record({"status": "Success"}))
    body = client.get("/samples/S1/?avi=asc").content.decode()
    assert body.index("CAGE:hepg2") < body.index("RNA_SEQ:liver") < body.index("SPLICE_SITES:brain")
    assert 'href="?avi=desc#alphagenome"' in body and 'aria-sort="ascending"' in body


def test_page_without_annotation_has_no_panel(client, storage):
    # Annotation is opt-in per run: most samples never have it, so their pages say nothing about it.
    sample_with(storage, run_record=record())
    body = client.get("/samples/S1/").content.decode()
    assert "Variants of interest" not in body and RESEARCH_NOTE not in body


def test_page_shows_a_failed_annotation_without_failing_the_sample(client, storage):
    sample_with(storage, run_record=record({"status": "Failure", "error": "API quota exceeded"}))
    response = client.get("/samples/S1/")
    body = response.content.decode()
    assert response.status_code == 200
    assert "AlphaGenome annotation failed for this run" in body and "API quota exceeded" in body


def test_page_escapes_the_tables_text(client, storage):
    tsv = "chrom\tpos\tref\talt\tgene\tavi\nsynth1\t1\tA\tG\t<script>x</script>\t0.5\n"
    sample_with(storage, tsv)
    body = client.get("/samples/S1/").content.decode()
    assert "<script>x</script>" not in body and "&lt;script&gt;x&lt;/script&gt;" in body


def test_no_panel_before_there_are_calls(client, storage):
    put(storage, "raw_reads/S1.fastq.gz", fastq_gz())
    put(storage, TSV_KEY, fixture_text())
    assert "Variants of interest" not in client.get("/samples/S1/").content.decode()


# ------------------------------------------------------------------ downloads


def test_kinds_storage_doesnt_name_are_never_offered_per_mate():
    # Before #207, keys_for() lacks the AlphaGenome names: they must not turn into alphagenome_tsv_R1-style kinds.
    kinds = [kind for kind, _, _ in _download_kinds({"raw": "raw_reads/S1.fastq.gz"})]
    assert not any(kind.startswith("alphagenome") and kind.endswith(("_R1", "_R2")) for kind in kinds)


def test_this_runs_annotation_is_offered_for_download(client, storage):
    sample_with(storage, fixture_text(), record({"status": "Success"}))
    put(storage, "variants/S1.alphagenome.vcf.gz", b"x")
    body = client.get("/samples/S1/").content.decode()
    assert "/samples/S1/files/alphagenome_tsv/" in body and "/samples/S1/files/alphagenome_vcf/" in body
    # #207's paths, before and after it adds the names to keys_for()
    assert b"AlphaGenomeTerms" in b"".join(client.get("/samples/S1/files/alphagenome_tsv/").streaming_content)


def test_an_earlier_runs_annotation_is_not_offered(client, storage):
    sample_with(storage, fixture_text(), record())  # the latest run didn't annotate
    put(storage, "variants/S1.alphagenome.vcf.gz", b"x")
    assert "alphagenome_tsv" not in client.get("/samples/S1/").content.decode()


def test_avi_ties_keep_the_files_rank():
    # #207 ranks AVI ties by |rna_lfc|, then QUAL: the page keeps that order rather than re-deriving it.
    text = "rank\tchrom\tpos\tref\talt\tavi\n2\tsynth1\t50\tC\tT\t20\n1\tsynth1\t900\tA\tG\t20\n"
    rows, problem = parse_tsv(text)
    assert problem == "" and [r.rank for r in rows] == [2, 1]
    assert [r.pos for r in sort_rows(rows)] == [900, 50]  # rank 1 first, though it's further along the contig


# ------------------------------------------------------------------ the hosted site (manage.py publish)


def published_page(storage, tmp_path, tsv=None, run_record=None):
    sample_with(storage, tsv, run_record)
    put(storage, "variants/S1.alphagenome.vcf.gz", b"x")
    put(storage, "qc_reports/S1.fastp.html", b"<html></html>")  # QC done too: a finished sample gets a page
    put(storage, "qc_reports/S1.fastp.json", json.dumps(FASTP_SINGLE))
    out = tmp_path / "site"
    publish_mod.publish(sample_ids=["S1"], out=str(out), api_base="https://api.example", log=lambda m: None)
    return (out / "samples" / "S1" / "index.html").read_text(encoding="utf-8")


def test_the_published_page_shows_the_table_with_the_terms_notice(storage, tmp_path):
    page = published_page(storage, tmp_path, fixture_text(), record({"status": "Success"}))
    assert "Variants of interest" in page and RESEARCH_NOTE in page
    assert "you agree to AlphaGenome Output Terms of Use found at" in page and f'href="{TERMS_URL}"' in page
    assert page.index("SPLICE_SITES:brain") < page.index("RNA_SEQ:liver")  # highest AVI first
    assert "?avi=" not in page  # a static page can't sort: no link that would only reload it


def test_the_published_page_offers_no_alphagenome_download(storage, tmp_path):
    # The hosted downloads are the web API's allowlist (PUBLIC_KINDS): AlphaGenome output is only the panel.
    page = published_page(storage, tmp_path, fixture_text(), record({"status": "Success"}))
    assert "/files/S1/alphagenome" not in page and "alphagenome.tsv" not in page


def test_a_sample_never_annotated_has_no_panel_on_the_published_page(storage, tmp_path):
    page = published_page(storage, tmp_path, run_record=record())
    assert "Variants of interest" not in page


def test_the_panel_adds_no_inline_style_so_the_csp_hashes_stay_the_same(storage, tmp_path):
    # The hosted CSP lists the inline blocks' hashes (a CloudFront apply to change): the panel uses site.css's classes.
    plain = publish_mod.csp_hashes({"p": published_page(storage, tmp_path / "a", run_record=record())})
    annotated = published_page(storage, tmp_path / "b", fixture_text(), record({"status": "Success"}))
    assert publish_mod.csp_hashes({"p": annotated}) == plain and ' style="' not in annotated
