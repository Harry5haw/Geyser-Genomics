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
