# 🖥️ Web UI

A small Django front end in `web/`. You can upload a gzipped FASTQ, or click **Generate demo sample** to create synthetic reads with known SNPs. Each stage turns green as it finishes. You then get a QC summary (read count, Q20/Q30, GC, duplication, insert size) with the fastp report, a table of called variants (with a "10/10 planted SNPs recovered" check for demo samples) and download links for every intermediate file.

```bash
docker build -f Dockerfile -t geyser-app .          # once: the pipeline image
pip install -r requirements-dev.txt                 # includes web/requirements.txt
python web/dev.py                                   # UI + local worker on http://127.0.0.1:8000
```

**How it fits the architecture.** The UI only reads and writes storage keys, through `app/storage.py`, and never runs the pipeline itself. An upload is stored at `raw_reads/<id>.fastq.gz`, or at `raw_reads/<id>_R1.fastq.gz` and `_R2.fastq.gz` when a second (R2) file is given. That key is the trigger, and a paired-end sample only starts once both mates exist:

| | Cloud | Offline |
| :--- | :--- | :--- |
| Storage | S3 (`STORAGE_BACKEND=s3`) | `local_bucket/` folder |
| Trigger | S3 event → Lambda → Step Functions | `manage.py runworker` polls `raw_reads/` → `scripts/run_local.py` |
| Stage status | from the output keys | from the output keys + `metrics/metrics.jsonl` + `runs/<id>.json` |

There is no database. Status is worked out from which outputs exist, the local metrics file and the worker's run records (`runs/<id>.json` and `.log`). The same UI could therefore run against the S3 bucket with no worker.

**Publishing it as a static site.** `manage.py publish` renders the same pages to plain files, for a host with no server (S3 + CloudFront):

```bash
cd web
python manage.py publish --all --out ../site                 # every sample (each publish also writes the 3D figures), into a folder
python -m http.server -d ../site                             # browse it: folders serve their index.html
python manage.py publish SRR062634 --out s3://<site bucket>  # one sample's pages and the list (a run's last step)
```

Each page is the live view rendered with a read-only flag, so the two can't drift apart. Only the public outputs are published (the VCF and its index, the QC report, the quality gate, the stats and the run record), never the reads or alignments. A sample's calls are published 50 per page, up to 20 pages; the VCF has the rest. Assets keep their content-hashed names (cached for a year), pages are cached for a minute, and `csp-hashes.json` lists the hashes of the inline script and styles for a strict Content-Security-Policy (no inline style attributes anywhere). With `--api-base` (or `GEYSER_API_BASE`), the downloads point at the web API's `/files/<id>/<kind>` and a running sample's row polls `/status/<id>`; without one the public files are copied next to the pages. With an API the site also gets an upload page (`/uploads/`): the browser sends gzipped FASTQ straight to the data lake in presigned multipart parts, 4 at a time, retrying a failed part and renewing URLs before they expire. An interrupted upload resumes from the parts S3 already has, even after closing the tab. The page also sets the run's quality-gate profile and reference genome. It needs the upload token, which stays in the page's memory and is never stored. The `geyser-publish` image (`app-publish/Dockerfile`, geyser-app plus the UI) runs it in the cloud.

<details>
<summary><b>Settings, safety and tests</b></summary>

<br/>

**Settings** (environment variables): `DJANGO_DEBUG` (default **off**), `DJANGO_SECRET_KEY` (required when DEBUG is off), `DJANGO_ALLOWED_HOSTS`, `DJANGO_HTTPS=1` behind TLS (secure cookies, HSTS, redirect), `GEYSER_MAX_UPLOAD_MB` (default 262144, i.e. 256 GB per file, as the hosted upload page), `REFERENCE_NAME` for the worker (default `synthetic_ref.fa`), and `GEYSER_RUNNER=docker|host`. The storage variables are the same as the pipeline's, except that the UI defaults to `local`.

**Safety.** CSRF protection is on. Sample IDs are restricted to `[A-Za-z0-9_-]` because they become storage keys. Uploads must be gzip and within the size limit, and a sample can't be overwritten. Downloads are limited to a fixed list of file types. The fastp report is shown in a sandboxed iframe with scripts blocked, so its charts are blank there (they load a script from opengene.org over plain HTTP); the downloaded report has them.

**Tests:** `pytest` runs the web tests too (`web/tests/`, Django test client, temporary storage folder, no Docker), including the publisher's: a published folder, served over HTTP, must read the same as the Django pages, and every link in it must resolve. `GEYSER_PUBLISH_E2E=1 pytest tests/test_e2e_publish.py` checks the image.

</details>

---

---

[← Back to the README](../README.md)
