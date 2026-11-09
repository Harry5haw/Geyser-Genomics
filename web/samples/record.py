"""The provenance run record (runs/<id>.provenance.json, app/provenance.py) as the detail page shows it:
what produced the sample's outputs (image, reference, tools) and how each stage went (status, time,
CPU, peak memory). Best effort: a record that can't be read leaves the section out."""

from dataclasses import dataclass, field

from geyser_web.pipeline import read_record


@dataclass
class RecordView:
    facts: list  # [(label, value)]
    stages: list  # [{name, status, duration, cpu, memory, run_name, error}]
    tools: list = field(default_factory=list)  # [(name, version)]
    status: str = None
    run_name: str = None


def _seconds(value):
    return f"{value:.1f} s" if isinstance(value, (int, float)) else ""


def load_record(storage, sample_id: str):
    """The sample's run record, or None (no record yet, or unreadable)."""
    try:
        return read_record(storage, sample_id)
    except Exception:  # noqa: BLE001 - the section is extra; never fail the sample page over it
        return None


def record_view(record):
    if not record:
        return None
    reference = record.get("reference") or {}
    facts = [
        ("Run", record.get("run_name") or "(no run name)"),
        ("Image", record.get("app_version")),
        ("Layout", record.get("layout")),
        ("Aligner", record.get("aligner")),
        ("Reference", reference.get("name")),
        ("Reference MD5", reference.get("md5")),
        ("Updated", record.get("updated")),
    ]
    stages = []
    for name, stage in sorted(
        (record.get("stages") or {}).items(), key=lambda item: (item[1] or {}).get("started") or ""
    ):
        stage = stage or {}
        usage = stage.get("resources") or {}
        memory = usage.get("peak_memory_mib")
        stages.append(
            {
                "name": name,
                "status": stage.get("status") or "",
                "duration": _seconds(stage.get("duration_seconds")),
                "cpu": _seconds(usage.get("cpu_seconds")),
                "memory": f"{memory:.0f} MiB" if isinstance(memory, (int, float)) else "",
                "run_name": stage.get("run_name") or "",
                "error": stage.get("error") or "",
            }
        )
    return RecordView(
        facts=[(label, value) for label, value in facts if value],
        stages=stages,
        tools=sorted((record.get("tools") or {}).items()),
        status=record.get("status"),
        run_name=record.get("run_name"),
    )


def _span(seconds):
    from .templatetags.geyser import duration

    return duration(seconds)


def deepvariant_view(record, state):
    """The DeepVariant card's facts, from the run record, when DeepVariant made (or is making) this run's calls.

    state: SampleView.deepvariant ("running" | "done" | None). Every fact is optional: older records have no
    make_examples or GPU notes (#251). DeepVariant's VCF stats are over PASS calls (#147)."""
    if not state:
        return None
    record = record or {}
    part = (record.get("stages") or {}).get("DeepVariant") or {}
    facts = []
    version = (record.get("tools") or {}).get("deepvariant")
    model = part.get("model_type") or (part.get("make_examples") or {}).get("model_type")
    if version or model:
        facts.append(("Model", " ".join(x for x in (f"DeepVariant {version}" if version else None, model) if x)))
    me = part.get("make_examples") or {}
    if me.get("split"):
        secs = [s for s in me.get("part_seconds") or [] if isinstance(s, (int, float))]
        each = f", {_span(min(secs))}–{_span(max(secs))} each" if secs else ""
        facts.append(("make_examples", f"{me.get('parts')} parts on CPU{each}"))
        if me.get("examples") is not None:
            facts.append(("Examples", f"{me['examples']:,}"))
    elif me.get("shards"):
        facts.append(("make_examples", f"{me['shards']} shards in one job"))
    gpu = part.get("gpu") or {}
    if gpu.get("model"):
        util = gpu.get("utilization_mean_pct")
        facts.append(
            ("call_variants", f"GPU {gpu['model']}" + (f", {util}% mean utilisation" if util is not None else ""))
        )
    if state == "done":
        facts.append(("bcftools fallback", "No: DeepVariant made the calls"))
        stats = (record.get("stats") or {}).get("bcftools_stats") or {}
        if stats.get("records") is not None:
            kinds = ", ".join(
                f"{stats[k]:,} {label}"
                for k, label in (("snps", "SNPs"), ("indels", "indels"))
                if stats.get(k) is not None
            )
            facts.append(("PASS calls", f"{stats['records']:,}" + (f" ({kinds})" if kinds else "")))
        if part.get("duration_seconds") is not None:
            facts.append(("Time", _span(part["duration_seconds"])))
        res = part.get("resources") or {}
        if res.get("cpu_seconds") is not None:
            facts.append(("CPU time", _span(res["cpu_seconds"])))
    return {"state": state, "facts": facts}
