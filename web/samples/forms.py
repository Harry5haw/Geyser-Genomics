from django import forms
from django.conf import settings

from geyser_web.pipeline import keys_for, parse_sample_id

from .runs import is_valid_sample_id

GZIP_MAGIC = b"\x1f\x8b"
ALLOWED_SUFFIXES = (".fastq.gz", ".fq.gz")
ID_HELP = "Letters, digits, '-' and '_' only (max 64). Leave blank to use the filename."


class UploadForm(forms.Form):
    reads = forms.FileField(label="Gzipped FASTQ")
    sample_id = forms.CharField(label="Sample ID", required=False, max_length=64, help_text=ID_HELP)

    def __init__(self, *args, storage=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage = storage

    @staticmethod
    def max_bytes() -> int:
        return settings.GEYSER_MAX_UPLOAD_MB * 1024 * 1024

    def clean_reads(self):
        f = self.cleaned_data["reads"]
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

    def clean(self):
        cleaned = super().clean()
        f = cleaned.get("reads")
        if f is None:
            return cleaned
        sample_id = (cleaned.get("sample_id") or "").strip()
        if not sample_id:
            try:
                sample_id = parse_sample_id(f.name)  # exactly what the cloud trigger will do
            except ValueError:
                sample_id = ""
        if not is_valid_sample_id(sample_id):
            self.add_error("sample_id", f"{sample_id!r} isn't a valid sample ID. {ID_HELP}")
            return cleaned
        if self.storage is not None and self.storage.exists(keys_for(sample_id)["raw"]):
            self.add_error("sample_id", f"A sample called {sample_id} already exists.")
            return cleaned
        cleaned["sample_id"] = sample_id
        return cleaned
