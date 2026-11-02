"""The headline numbers of a sample's fastp report (qc_reports/<id>.fastp.json), for the detail page.

fastp's HTML report draws its charts with JavaScript, which the sandboxed report view blocks, so the
page shows these numbers from the JSON as a table above the report.
"""

import json

MAX_QC_JSON_BYTES = 20 * 1024 * 1024  # a fastp JSON is ~50-100 KB; anything far bigger isn't one


def _percent(value) -> str:
    return f"{100 * float(value):.1f}%"


def read_qc_summary(storage, key: str) -> list | None:
    """[(label, value), ...] from a fastp JSON report, or None if it is missing or unreadable.

    Best effort: the table is extra, so any storage error (missing object, AccessDenied, a dropped
    connection) leaves it out rather than failing the sample page.
    """
    try:
        with storage.open_read(key) as fh:
            raw = fh.read(MAX_QC_JSON_BYTES + 1)
    except Exception:  # noqa: BLE001 - see above
        return None
    if len(raw) > MAX_QC_JSON_BYTES:
        return None
    try:
        report = json.loads(raw)
        before = report["summary"]["before_filtering"]
        paired = "read2_mean_length" in before
        rows = [
            ("Reads", f"{int(before['total_reads']):,}" + (" (both mates)" if paired else "")),
            ("Bases", f"{int(before['total_bases']):,}"),
            ("Mean read length", f"{before['read1_mean_length']}"
             + (f" / {before['read2_mean_length']} (R1 / R2)" if paired else "")),
            ("Bases ≥ Q20", _percent(before["q20_rate"])),
            ("Bases ≥ Q30", _percent(before["q30_rate"])),
            ("GC content", _percent(before["gc_content"])),
        ]
        if "duplication" in report:
            rows.append(("Duplication (fastp estimate)", _percent(report["duplication"]["rate"])))
        if paired and report.get("insert_size", {}).get("peak"):
            rows.append(("Insert size (peak)", f"{int(report['insert_size']['peak'])} bp"))
        return rows
    except (ValueError, KeyError, TypeError):
        return None
