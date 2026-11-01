from django import forms
from django.conf import settings

from geyser_web.pipeline import MATES, PAIRED, SINGLE, keys_for, parse_sample_id

from .runs import is_valid_sample_id

GZIP_MAGIC = b"\x1f\x8b"
ALLOWED_SUFFIXES = (".fastq.gz", ".fq.gz")
ID_HELP = "Letters, digits, '-' and '_' only (max 64). Leave blank to use the filename."


class UploadForm(forms.Form):
    reads = forms.FileField(label="Gzipped FASTQ")
    reads_r2 = forms.FileField(label="Read 2 (paired-end)", required=False)
    sample_id = forms.CharField(label="Sample ID", required=False, max_length=64, help_text=ID_HELP)

    def __init__(self, *args, storage=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage = storage

    @staticmethod
    def max_bytes() -> int:
        """Per file: a paired-end upload may be up to twice this in total."""
        return settings.GEYSER_MAX_UPLOAD_MB * 1024 * 1024

    def _check_fastq_gz(self, f):
        if not f.name.lower().endswith(ALLOWED_SUFFIXES):
            raise forms.ValidationError("The file must be a gzipped FASTQ ending in .fastq.gz or .fq.gz.")
        if f.size > self.max_bytes():
            raise forms.ValidationError(f"The file is larger than the {settings.GEYSER_MAX_UPLOAD_MB} MB limit.")
        f.seek(0)
        magic = f.read(2)
        f.seek(0)
        if magic != GZIP_MAGIC:
            raise forms.ValidationError("That file isn't gzip-compressed.")
        return f

    def clean_reads(self):
        return self._check_fastq_gz(self.cleaned_data["reads"])

    def clean_reads_r2(self):
        f = self.cleaned_data.get("reads_r2")
        return self._check_fastq_gz(f) if f else None

    def _taken(self, sample_id, layout):
        """Any raw key of either layout claims the ID, except that a paired upload may replace a
        lone mate: that is what a failed earlier paired upload leaves behind, and it never ran."""
        single = keys_for(sample_id, SINGLE)["raw"]
        mates = [keys_for(sample_id, PAIRED)[f"raw_{m}"] for m in MATES]
        if self.storage.exists(single):
            return True
        present = [k for k in mates if self.storage.exists(k)]
        return len(present) == len(mates) if layout == PAIRED else bool(present)

    def clean(self):
        cleaned = super().clean()
        f = cleaned.get("reads")
        if f is None or self.has_error("reads_r2"):
            return cleaned
        layout = PAIRED if cleaned.get("reads_r2") else SINGLE
        sample_id = (cleaned.get("sample_id") or "").strip()
        if not sample_id:
            try:
                # exactly what the cloud trigger will do: X.fastq.gz and X_R1.fastq.gz both give X
                sample_id = parse_sample_id(f.name)
            except ValueError:
                sample_id = ""
        if not is_valid_sample_id(sample_id):
            self.add_error("sample_id", f"{sample_id!r} isn't a valid sample ID. {ID_HELP}")
            return cleaned
        if sample_id.endswith(tuple(f"_{m}" for m in MATES)):
            # raw_reads/X_R1.fastq.gz is read as mate R1 of sample X, so it can't be a sample's own name.
            self.add_error("sample_id", f"Sample IDs can't end in _R1 or _R2: those mark paired-end mates. {ID_HELP}")
            return cleaned
        if self.storage is not None and self._taken(sample_id, layout):
            self.add_error("sample_id", f"A sample called {sample_id} already exists.")
            return cleaned
        cleaned["sample_id"] = sample_id
        cleaned["layout"] = layout
        return cleaned
