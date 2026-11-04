// The hosted site's upload page (/uploads/): gzipped FASTQ straight from the browser to the data lake, in
// presigned multipart parts (the web API's POST /uploads, /uploads/parts, /uploads/complete, /uploads/abort).
//
// - The upload token stays in this page's memory (the input field) and is sent only as Authorization: Bearer to
//   the API. It is never stored: a resumed upload asks for it again.
// - Up to 4 parts are in flight at a time. Each part is retried with backoff (and waits for the network to come
//   back), and part URLs are renewed before they expire (a big file can take longer than their hour), at most
//   1,000 at a time. The first part that fails for good stops the others.
// - Every file's parts are sent before any file is completed, and the files are then completed back to back: a
//   paired sample's mates land together, so the trigger (which starts a paired run once both mates exist) sees
//   the new R1 with the new R2, not with an R2 left from an earlier upload.
// - Resumable: localStorage keeps what's needed to carry on after a reload, a closed tab or a sleep: the upload
//   ids, part size, the files' name, size and date, and which files are complete; never the token or any data.
//   Choosing the same files again continues from the parts S3 already has (it says which, via /uploads/parts).
// The core (GeyserUpload.sendParts and friends) takes its I/O as arguments, so web/tests/js/ tests it in node.
(() => {
  "use strict";

  const CONCURRENCY = 4;
  const MAX_TRIES = 6;
  const RENEW_MARGIN_MS = 5 * 60 * 1000; // ask for fresh part URLs this long before they expire
  const RENEW_BATCH = 1000; // part URLs per /uploads/parts call (the API's limit)
  // API answers retrying can't help: a bad request, the token, the upload is gone. (S3 PUTs differ: a 400 there is
  // RequestTimeout or ExpiredToken, both fixed by a fresh URL and another try; only a 404 means the upload is gone.)
  const FATAL = new Set([400, 401, 404]);
  const PROGRESS_SAY_MS = 2000; // the status line's progress text at most this often (screen readers read it)
  const SAMPLE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$/; // as the web API (and the trigger) accept
  const MATE_LIKE = /_R[12]$/;
  const STORE = "geyser-upload:";
  const ENDED = new Set(["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"]);
  const FOLLOW_EVERY_MS = 15000;
  const FOLLOW_FOR_MS = 10 * 60 * 1000; // how long to wait for the run to appear before saying so

  class ApiError extends Error {
    constructor(status, message, fromApi = false) {
      super(message);
      this.status = status;
      this.fromApi = fromApi; // the web API's answer (not an S3 PUT's)
    }
  }
  const fatal = (err) => (err.fromApi ? FATAL.has(err.status) : err.status === 404);

  const checkSampleId = (id) => {
    if (!SAMPLE_ID.test(id)) return "Use 1-30 letters, digits, '-' or '_', starting with a letter or digit.";
    if (MATE_LIKE.test(id)) return "A sample ID can't end in _R1 or _R2 (that marks a mate).";
    return "";
  };

  // "HG003_R1.fastq.gz" -> "HG003": the ID a file suggests (the user can change it).
  const idFromName = (name) =>
    name
      .replace(/\.(fastq|fq)\.gz$/i, "")
      .replace(/_R[12]$/, "")
      .replace(/[^A-Za-z0-9_-]/g, "-")
      .replace(/^[^A-Za-z0-9]+/, "")
      .slice(0, 30);

  const partRanges = (size, partSize) => {
    const ranges = [];
    for (let n = 1, start = 0; start < size; n += 1, start += partSize) {
      ranges.push({ part_number: n, start, end: Math.min(size, start + partSize) });
    }
    return ranges;
  };

  // The API client: JSON in and out, the token as a bearer header, errors as ApiError with the API's message.
  const apiClient = (base, token, fetchImpl) => async (path, body) => {
    let response;
    try {
      response = await fetchImpl(`${base}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
        cache: "no-store",
      });
    } catch (_) {
      throw new ApiError(0, "The upload service can't be reached. Check the connection and try again.");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new ApiError(response.status, data.error || data.message || `HTTP ${response.status}`, true);
    }
    return data;
  };

  // One file's parts: PUT every part not yet `done`, renewing URLs before they expire. Returns the part list for
  // /uploads/complete (the caller completes, once every file's parts are in).
  // io: {api(path, body), put(url, blob, onBytes) -> etag, sleep(ms), now(), online() -> Promise, signal}
  const sendParts = async (io, file, upload, done, onProgress) => {
    const ranges = partRanges(file.size, upload.part_size);
    const etags = new Map(done); // part_number -> etag
    // part_number -> {url, expiresAt}: each URL lives from when it was signed (renewals sign some, not all).
    const urls = new Map(
      (upload.parts || []).map((p) => [p.part_number, { url: p.url, expiresAt: upload.expires_at || 0 }]),
    );
    const sent = new Map(); // part_number -> bytes sent (for progress)
    for (const r of ranges) if (etags.has(r.part_number)) sent.set(r.part_number, r.end - r.start);
    const report = () => onProgress([...sent.values()].reduce((a, b) => a + b, 0), file.size);
    report();

    const fresh = (n) => urls.has(n) && io.now() <= urls.get(n).expiresAt - RENEW_MARGIN_MS;
    // Fresh URLs for up to RENEW_BATCH parts still to send, from part `from` on (one call at a time).
    let renewing = null;
    const renew = async (from) => {
      while (renewing) await renewing.catch(() => {});
      if (fresh(from)) return; // another worker's call covered it
      renewing = (async () => {
        const wanted = ranges
          .map((r) => r.part_number)
          .filter((n) => n >= from && !etags.has(n))
          .slice(0, RENEW_BATCH);
        const answer = await io.api("/uploads/parts", {
          key: upload.key,
          upload_id: upload.upload_id,
          part_numbers: wanted,
        });
        const expiresAt = io.now() + (answer.expires_in || 0) * 1000;
        for (const p of answer.parts || []) urls.set(p.part_number, { url: p.url, expiresAt });
      })().finally(() => {
        renewing = null;
      });
      await renewing;
    };

    let stopped = null; // the first error that ends this file: the other workers stop taking parts
    const cancelled = () => io.signal && io.signal.aborted;
    const queue = ranges.filter((r) => !etags.has(r.part_number));
    const sendPart = async (r) => {
      for (let tries = 1; ; tries += 1) {
        if (cancelled()) throw new ApiError(0, "cancelled");
        if (stopped) throw stopped;
        try {
          if (!fresh(r.part_number)) await renew(r.part_number);
          const etag = await io.put(urls.get(r.part_number).url, file.slice(r.start, r.end), (bytes) => {
            sent.set(r.part_number, bytes);
            report();
          });
          etags.set(r.part_number, etag);
          sent.set(r.part_number, r.end - r.start);
          report();
          return;
        } catch (err) {
          sent.set(r.part_number, 0);
          report();
          if (cancelled() || stopped || fatal(err) || tries >= MAX_TRIES) throw err;
          // An S3 403/400: an expired URL or signing token, or a stalled connection: a fresh URL, then retry.
          if (!err.fromApi && (err.status === 403 || err.status === 400)) urls.delete(r.part_number);
          await io.online();
          await io.sleep(Math.min(30000, 1000 * 2 ** (tries - 1)));
        }
      }
    };
    const worker = async () => {
      while (queue.length && !stopped) {
        try {
          await sendPart(queue.shift());
        } catch (err) {
          stopped ||= err;
          throw err;
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, queue.length) }, worker));
    return ranges.map((r) => ({ part_number: r.part_number, etag: etags.get(r.part_number) }));
  };

  const completeUpload = (io, upload, parts) =>
    io.api("/uploads/complete", { key: upload.key, upload_id: upload.upload_id, parts });

  // One file, start to finish (a single-file upload; the page completes several files together instead).
  const sendFile = async (io, file, upload, done, onProgress) => {
    const parts = await sendParts(io, file, upload, done, onProgress);
    await completeUpload(io, upload, parts);
    return parts;
  };

  // What S3 already has of an unfinished upload: part_number -> etag for the parts of the right size. Asks for
  // no URLs (sendParts asks for them as it goes, a batch at a time).
  const partsDone = async (io, file, saved) => {
    const ranges = new Map(partRanges(file.size, saved.part_size).map((r) => [r.part_number, r.end - r.start]));
    const answer = await io.api("/uploads/parts", { key: saved.key, upload_id: saved.upload_id, part_numbers: [] });
    const done = new Map();
    for (const p of answer.done || []) {
      if (ranges.get(p.part_number) === p.size) done.set(p.part_number, p.etag);
    }
    return { upload: { ...saved, parts: [], expires_at: 0 }, done };
  };

  const sameFile = (file, saved) =>
    file.name === saved.name && file.size === saved.size && file.lastModified === saved.lastModified;

  // Is this /status answer the run of the upload that finished at `since` (ms), not an earlier run of the sample?
  // A minute's allowance for the browser's clock.
  const runOfUpload = (run, since) => {
    const started = Date.parse((run && run.started) || "");
    return !Number.isNaN(started) && started >= since - 60 * 1000;
  };

  // ------------------------------------------------------------------------------------------------- the page
  const mount = (doc) => {
    const form = doc.getElementById("upload-form");
    const $ = (id) => doc.getElementById(id);
    const api = form.dataset.api;
    const existing = new Set((form.dataset.existing || "").split(" ").filter(Boolean));
    const status = $("up-status"), bar = $("up-progress"), cancel = $("up-cancel"), pending = $("up-pending");
    const submit = form.querySelector("button[type=submit]");
    const fields = ["up-token", "up-r1", "up-r2", "up-id"].map($);
    let current = null; // the running upload's AbortController
    let confirmedReplace = "", busy = false, followed = 0;

    const say = (text, kind, field) => {
      status.textContent = text;
      status.className = kind || "";
      for (const f of fields) f.removeAttribute("aria-invalid");
      if (field) {
        field.setAttribute("aria-invalid", "true");
        field.focus();
      }
    };
    const layout = () => form.querySelector("input[name=layout]:checked").value;
    const files = () => [$("up-r1").files[0], layout() === "paired" ? $("up-r2").files[0] : null].filter(Boolean);
    const load = () => {
      const out = [];
      for (let i = 0; i < localStorage.length; i += 1) {
        const k = localStorage.key(i);
        if (k.startsWith(STORE)) {
          try {
            out.push(JSON.parse(localStorage.getItem(k)));
          } catch (_) {
            localStorage.removeItem(k);
          }
        }
      }
      return out;
    };
    const store = (entry) => localStorage.setItem(STORE + entry.sample_id, JSON.stringify(entry));
    const forget = (sampleId) => localStorage.removeItem(STORE + sampleId);
    const leaving = (e) => e.preventDefault(); // while uploading: the browser asks first (it can resume anyway)
    const setBusy = (on) => {
      busy = on;
      submit.disabled = on;
      cancel.hidden = !on;
      if (on) addEventListener("beforeunload", leaving);
      else removeEventListener("beforeunload", leaving);
      showPending();
    };

    const showPending = () => {
      pending.replaceChildren();
      for (const entry of load()) {
        const p = doc.createElement("p");
        const names = entry.files.map((f) => f.name).join(" + ");
        const many = entry.files.length > 1 ? "s" : "";
        p.textContent = `Unfinished upload of ${entry.sample_id} (${names}). To carry on, enter the token, choose the same file${many} under that sample ID and press Upload. `;
        if (!busy) {
          // Not while uploading: discarding the upload in progress would pull it out from under the page.
          const discard = doc.createElement("button");
          discard.type = "button";
          discard.textContent = "Discard it";
          discard.addEventListener("click", () => discardEntry(entry));
          p.append(discard);
        }
        pending.append(p);
      }
      pending.hidden = !pending.childElementCount;
    };

    const io = (ctrl) => {
      const token = $("up-token").value.trim();
      return {
        api: apiClient(api, token, fetch.bind(globalThis)),
        put: (url, blob, onBytes) =>
          new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            const abort = () => xhr.abort();
            ctrl.signal.addEventListener("abort", abort, { once: true });
            const settle = () => ctrl.signal.removeEventListener("abort", abort);
            xhr.open("PUT", url);
            xhr.upload.onprogress = (e) => onBytes(e.loaded);
            xhr.onload = () => {
              settle();
              const etag = xhr.getResponseHeader("ETag");
              if (xhr.status >= 200 && xhr.status < 300 && etag) resolve(etag);
              else reject(new ApiError(xhr.status, etag ? `part upload: HTTP ${xhr.status}` : "no ETag in the reply"));
            };
            xhr.onerror = xhr.onabort = () => {
              settle();
              reject(new ApiError(0, "part upload interrupted"));
            };
            xhr.send(blob);
          }),
        sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
        now: () => Date.now(),
        online: () =>
          navigator.onLine ? Promise.resolve() : new Promise((r) => addEventListener("online", r, { once: true })),
        signal: ctrl.signal,
      };
    };

    const discardEntry = async (entry) => {
      if (busy) return;
      if (!$("up-token").value.trim()) return say("Enter the upload token to discard an upload.", "error", $("up-token"));
      const call = io(new AbortController()).api;
      for (const f of entry.files) {
        if (f.completed) continue; // already in raw_reads/: nothing to abort
        try {
          await call("/uploads/abort", { key: f.key, upload_id: f.upload_id });
        } catch (err) {
          if (err.status !== 404) return say(err.message, "error");
        }
      }
      forget(entry.sample_id);
      showPending();
      say(`Discarded the unfinished upload of ${entry.sample_id}.`);
    };

    // After the upload: this upload's run, from the API every 15 s, until it ends (or doesn't show up).
    const follow = async (sampleId, since) => {
      const mine = (followed += 1); // a later upload stops this one following
      const url = `${api}/status/${encodeURIComponent(sampleId)}`;
      while (mine === followed) {
        await new Promise((r) => setTimeout(r, FOLLOW_EVERY_MS));
        if (mine !== followed) return;
        let run = null;
        try {
          const r = await fetch(url, { cache: "no-store" });
          run = r.ok ? await r.json() : null;
        } catch (_) {
          // keep following; the status is a convenience
        }
        if (mine !== followed) return;
        if (run && runOfUpload(run, since)) {
          if (ENDED.has(run.status)) {
            const a = doc.createElement("a");
            a.href = `/samples/${encodeURIComponent(sampleId)}/`;
            a.textContent = "its page";
            say(`${sampleId}: the run ${run.status === "SUCCEEDED" ? "succeeded" : "ended: " + run.status}. See `);
            status.append(a, " (published a minute or so after the run).");
            return;
          }
          say(`${sampleId}: uploaded; the run is ${String(run.status).toLowerCase()}.`);
        } else if (Date.now() - since > FOLLOW_FOR_MS) {
          say(`${sampleId}: uploaded, but no run has started yet. Check the sample list later.`);
          return;
        }
      }
    };

    $("up-r1").addEventListener("change", () => {
      const f = $("up-r1").files[0];
      if (f && !$("up-id").value) $("up-id").value = idFromName(f.name);
    });
    form.querySelectorAll("input[name=layout]").forEach((r) =>
      r.addEventListener("change", () => {
        $("up-r2-field").hidden = layout() !== "paired";
      }),
    );
    cancel.addEventListener("click", () => current && current.abort());

    const upload = async () => {
      const sampleId = $("up-id").value.trim(), chosen = files(), paired = layout() === "paired";
      if (!$("up-token").value.trim()) return say("Enter the upload token.", "error", $("up-token"));
      const idProblem = checkSampleId(sampleId);
      if (idProblem) return say(idProblem, "error", $("up-id"));
      if (!$("up-r1").files[0]) return say("Choose the file.", "error", $("up-r1"));
      if (paired && !$("up-r2").files[0]) return say("Choose the R2 file too.", "error", $("up-r2"));
      for (const [i, f] of chosen.entries()) {
        const field = $(i ? "up-r2" : "up-r1");
        if (!/\.(fastq|fq)\.gz$/i.test(f.name)) return say("Files must be gzipped FASTQ (.fastq.gz or .fq.gz).", "error", field);
        const magic = new Uint8Array(await f.slice(0, 2).arrayBuffer());
        if (magic[0] !== 0x1f || magic[1] !== 0x8b) return say(`${f.name} isn't gzip-compressed.`, "error", field);
      }
      const entry = load().find((s) => s.sample_id === sampleId);
      const resuming = !!entry && entry.files.length === chosen.length && entry.files.every((s, i) => sameFile(chosen[i], s));
      if (entry && !resuming) {
        return say(`${sampleId} has an unfinished upload of other files: choose those to carry on, or discard it.`, "error", $("up-r1"));
      }
      if (!resuming && existing.has(sampleId) && confirmedReplace !== sampleId) {
        confirmedReplace = sampleId;
        return say(`${sampleId} is already on the site: uploading replaces its reads and runs it again. Press Upload again to go ahead.`, "error");
      }

      followed += 1; // stop following an earlier upload: the status line is this one's now
      const ctrl = (current = new AbortController());
      const run = io(ctrl);
      // Progress: the bytes of every file; the rate only from what this session has sent.
      const total = chosen.reduce((a, f) => a + f.size, 0), sentBy = chosen.map(() => 0);
      const firstBy = chosen.map(() => null), began = Date.now();
      let saidAt = 0;
      const progress = (i) => (bytes) => {
        sentBy[i] = bytes;
        if (firstBy[i] === null) firstBy[i] = bytes;
        const sum = sentBy.reduce((a, b) => a + b, 0);
        const fresh = sentBy.reduce((a, b, j) => a + b - (firstBy[j] ?? b), 0), secs = (Date.now() - began) / 1000;
        bar.value = total ? sum / total : 0;
        if (Date.now() - saidAt < PROGRESS_SAY_MS && sum < total) return; // the bar moves; the words less often
        saidAt = Date.now();
        const rate = secs > 2 && fresh > 0 ? fresh / secs : 0;
        const left = rate ? Math.round((total - sum) / rate / 60) : null;
        say(`Uploading ${sampleId}: ${(100 * bar.value).toFixed(0)}%` + (rate ? ` · ${(rate / 1048576).toFixed(1)} MB/s` : "") + (left !== null ? ` · about ${left} min left` : ""));
      };

      let saved = entry, started = resuming; // an upload exists to carry on with (its parts are kept on failure)
      try {
        const plans = [];
        if (resuming) {
          for (const [i, s] of saved.files.entries()) {
            if (s.completed) {
              progress(i)(chosen[i].size);
              plans.push(null);
            } else {
              plans.push(await partsDone(run, chosen[i], s));
            }
          }
        } else {
          const metadata = {};
          if ($("up-gate").value) metadata["gate-profile"] = $("up-gate").value;
          if ($("up-reference").value) metadata.reference = $("up-reference").value;
          const begun = await run.api("/uploads", { sample_id: sampleId, layout: layout(), sizes: chosen.map((f) => f.size), metadata });
          const at = Date.now() + begun.expires_in * 1000;
          saved = {
            sample_id: sampleId,
            created: new Date().toISOString(),
            files: begun.uploads.map((u, i) => ({
              key: u.key,
              upload_id: u.upload_id,
              part_size: u.part_size,
              name: chosen[i].name,
              size: chosen[i].size,
              lastModified: chosen[i].lastModified,
              completed: false,
            })),
          };
          store(saved);
          started = true;
          showPending();
          plans.push(...begun.uploads.map((u) => ({ upload: { ...u, expires_at: at }, done: new Map() })));
        }
        // Every file's parts first, then the files completed back to back (a pair's mates land together).
        const partLists = [];
        for (const [i, plan] of plans.entries()) {
          partLists.push(plan ? await sendParts(run, chosen[i], plan.upload, plan.done, progress(i)) : null);
        }
        for (const [i, plan] of plans.entries()) {
          if (!plan) continue;
          await completeUpload(run, plan.upload, partLists[i]);
          saved.files[i].completed = true; // a resume after a failure here skips this file
          store(saved);
        }
        forget(sampleId);
        bar.value = 1;
        say(`Uploaded ${sampleId}. The run starts in a few seconds; this page follows it.`, "ok");
        follow(sampleId, Date.now());
      } catch (err) {
        const wasCancelled = ctrl.signal.aborted;
        ctrl.abort(); // stop any part still in flight
        if (wasCancelled) {
          say("Cancelled. The parts sent so far are kept: choose the same files to carry on, or discard the upload.");
        } else if (resuming && err.status === 404) {
          // One of its uploads is gone (finished, discarded or expired; perhaps completed with the reply lost).
          // Drop the others too, so no unfinished upload is left behind.
          for (const f of saved.files) {
            if (!f.completed) run.api("/uploads/abort", { key: f.key, upload_id: f.upload_id }).catch(() => {});
          }
          forget(sampleId);
          say("That unfinished upload is gone (finished, discarded or expired). Press Upload to start again.", "error");
        } else if (started) {
          say(`${err.message} The parts sent so far are kept: press Upload again to carry on.`, "error");
        } else {
          say(err.message, "error");
        }
      }
    };

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (busy) return; // set before any wait, so a double click can't start two uploads
      setBusy(true);
      try {
        await upload();
      } finally {
        current = null;
        setBusy(false);
      }
    });

    showPending();
  };

  const exported = {
    ApiError,
    apiClient,
    checkSampleId,
    completeUpload,
    idFromName,
    partRanges,
    partsDone,
    runOfUpload,
    sameFile,
    sendFile,
    sendParts,
  };
  globalThis.GeyserUpload = exported;
  if (typeof document !== "undefined" && document.getElementById("upload-form")) {
    if (document.prerendering) document.addEventListener("prerenderingchange", () => mount(document), { once: true });
    else mount(document);
  }
})();
