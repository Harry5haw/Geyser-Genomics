// The hosted site's upload page (/uploads/): gzipped FASTQ straight from the browser to the data lake, in
// presigned multipart parts (the web API's POST /uploads, /uploads/parts, /uploads/complete, /uploads/abort).
//
// - The upload token stays in this page's memory (the input field) and is sent only as Authorization: Bearer to
//   the API. It is never stored: a resumed upload asks for it again.
// - Up to 4 parts are in flight at a time. Each part is retried with backoff (and waits for the network to come
//   back), and part URLs are renewed before they expire (a big file can take longer than their hour).
// - Resumable: localStorage keeps what's needed to carry on after a reload, a closed tab or a sleep: the upload
//   ids, part size and the files' name, size and date, never the token or any data. Choosing the same files
//   again continues from the parts S3 already has (it says which, via /uploads/parts).
// The core (GeyserUpload.sendFile and friends) takes its I/O as arguments, so web/tests/js/ tests it in node.
(() => {
  "use strict";

  const CONCURRENCY = 4;
  const MAX_TRIES = 6;
  const RENEW_MARGIN_MS = 5 * 60 * 1000; // ask for fresh part URLs this long before they expire
  const RENEW_BATCH = 1000; // part URLs per /uploads/parts call (the API's limit)
  const SAMPLE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$/; // as the web API (and the trigger) accept
  const MATE_LIKE = /_R[12]$/;
  const STORE = "geyser-upload:";
  const ENDED = new Set(["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"]);

  class ApiError extends Error {
    constructor(status, message) {
      super(message);
      this.status = status;
    }
  }

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
    if (!response.ok) throw new ApiError(response.status, data.error || data.message || `HTTP ${response.status}`);
    return data;
  };

  // One file's parts: PUT every part not yet `done`, renewing URLs before they expire, then complete the upload.
  // io: {api(path, body), put(url, blob, onBytes) -> etag, sleep(ms), now(), online() -> Promise, signal}
  const sendFile = async (io, file, upload, done, onProgress) => {
    const ranges = partRanges(file.size, upload.part_size);
    const etags = new Map(done); // part_number -> etag
    // part_number -> {url, expiresAt}: each URL lives from when it was signed (renewals sign some, not all).
    const urls = new Map((upload.parts || []).map((p) => [p.part_number, { url: p.url, expiresAt: upload.expires_at || 0 }]));
    const sent = new Map(); // part_number -> bytes sent (for progress)
    for (const r of ranges) if (etags.has(r.part_number)) sent.set(r.part_number, r.end - r.start);
    const report = () => onProgress([...sent.values()].reduce((a, b) => a + b, 0), file.size);
    report();

    // Fresh URLs for up to RENEW_BATCH parts still to send, from part `from` on (one call at a time).
    let renewing = null;
    const renew = async (from) => {
      while (renewing) await renewing;
      if (urls.has(from) && io.now() <= urls.get(from).expiresAt - RENEW_MARGIN_MS) return; // another call did it
      renewing = (async () => {
        const wanted = ranges
          .map((r) => r.part_number)
          .filter((n) => n >= from && !etags.has(n))
          .slice(0, RENEW_BATCH);
        const fresh = await io.api("/uploads/parts", {
          key: upload.key,
          upload_id: upload.upload_id,
          part_numbers: wanted,
        });
        const expiresAt = io.now() + (fresh.expires_in || 0) * 1000;
        for (const p of fresh.parts || []) urls.set(p.part_number, { url: p.url, expiresAt });
      })().finally(() => {
        renewing = null;
      });
      await renewing;
    };
    const fresh = (n) => urls.has(n) && io.now() <= urls.get(n).expiresAt - RENEW_MARGIN_MS;

    const queue = ranges.filter((r) => !etags.has(r.part_number));
    const sendPart = async (r) => {
      for (let tries = 1; ; tries += 1) {
        if (io.signal && io.signal.aborted) throw new ApiError(0, "cancelled");
        if (!fresh(r.part_number)) await renew(r.part_number);
        try {
          const etag = await io.put(urls.get(r.part_number).url, file.slice(r.start, r.end), (bytes) => {
            sent.set(r.part_number, bytes);
            report();
          });
          etags.set(r.part_number, etag);
          sent.set(r.part_number, r.end - r.start);
          report();
          return;
        } catch (err) {
          if ((io.signal && io.signal.aborted) || tries >= MAX_TRIES) throw err;
          sent.set(r.part_number, 0);
          report();
          if (err.status === 403) urls.delete(r.part_number); // most likely an expired URL: renew it
          await io.online();
          await io.sleep(Math.min(30000, 1000 * 2 ** (tries - 1)));
        }
      }
    };
    const worker = async () => {
      while (queue.length) await sendPart(queue.shift());
    };
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, queue.length) }, worker));

    const parts = ranges.map((r) => ({ part_number: r.part_number, etag: etags.get(r.part_number) }));
    await io.api("/uploads/complete", { key: upload.key, upload_id: upload.upload_id, parts });
  };

  // What S3 already has of an unfinished upload: part_number -> etag for the parts of the right size. Asks for
  // no URLs (sendFile asks for them as it goes, a batch at a time).
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

  // ------------------------------------------------------------------------------------------------- the page
  const mount = (doc) => {
    const form = doc.getElementById("upload-form");
    const $ = (id) => doc.getElementById(id);
    const api = form.dataset.api;
    const existing = new Set((form.dataset.existing || "").split(" ").filter(Boolean));
    const status = $("up-status"), bar = $("up-progress"), cancel = $("up-cancel"), pending = $("up-pending");
    const submit = form.querySelector("button[type=submit]");
    let controller = null, confirmedReplace = "", busy = false;

    const say = (text, kind) => {
      status.textContent = text;
      status.className = kind || "";
    };
    const layout = () => form.querySelector("input[name=layout]:checked").value;
    const files = () => [$("up-r1").files[0], layout() === "paired" ? $("up-r2").files[0] : null].filter(Boolean);
    const saved = () => {
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

    const showPending = () => {
      pending.replaceChildren();
      for (const entry of saved()) {
        const p = doc.createElement("p");
        const names = entry.files.map((f) => f.name).join(" + ");
        p.textContent = `Unfinished upload of ${entry.sample_id} (${names}). To carry on, enter the token, choose the same file${entry.files.length > 1 ? "s" : ""} under that sample ID and press Upload. `;
        const discard = doc.createElement("button");
        discard.type = "button";
        discard.textContent = "Discard it";
        discard.addEventListener("click", () => discardEntry(entry));
        p.append(discard);
        pending.append(p);
      }
      pending.hidden = !pending.childElementCount;
    };

    const io = () => {
      const token = $("up-token").value.trim();
      return {
        api: apiClient(api, token, fetch.bind(globalThis)),
        put: (url, blob, onBytes) =>
          new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            const abort = () => xhr.abort();
            controller.signal.addEventListener("abort", abort, { once: true });
            xhr.open("PUT", url);
            xhr.upload.onprogress = (e) => onBytes(e.loaded);
            xhr.onload = () => {
              controller.signal.removeEventListener("abort", abort);
              const etag = xhr.getResponseHeader("ETag");
              if (xhr.status >= 200 && xhr.status < 300 && etag) resolve(etag);
              else reject(new ApiError(xhr.status, etag ? `part upload: HTTP ${xhr.status}` : "no ETag in the reply"));
            };
            xhr.onerror = xhr.onabort = () => reject(new ApiError(0, "part upload interrupted"));
            xhr.send(blob);
          }),
        sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
        now: () => Date.now(),
        online: () =>
          navigator.onLine ? Promise.resolve() : new Promise((r) => addEventListener("online", r, { once: true })),
        signal: controller.signal,
      };
    };

    const discardEntry = async (entry) => {
      if (!$("up-token").value.trim()) return say("Enter the upload token to discard an upload.", "error");
      controller = new AbortController();
      const call = io().api;
      for (const f of entry.files) {
        try {
          await call("/uploads/abort", { key: f.key, upload_id: f.upload_id });
        } catch (err) {
          if (err.status !== 404) return say(err.message, "error");
        }
      }
      localStorage.removeItem(STORE + entry.sample_id);
      showPending();
      say(`Discarded the unfinished upload of ${entry.sample_id}.`);
    };

    const follow = async (sampleId) => {
      // After the upload: the run's status from the API, every 15 s, until it ends.
      const url = `${api}/status/${encodeURIComponent(sampleId)}`;
      for (;;) {
        await new Promise((r) => setTimeout(r, 15000));
        try {
          const r = await fetch(url, { cache: "no-store" });
          const run = r.ok ? await r.json() : null;
          if (run && ENDED.has(run.status)) {
            const a = doc.createElement("a");
            a.href = `/samples/${encodeURIComponent(sampleId)}/`;
            a.textContent = "its page";
            say(`${sampleId}: the run ${run.status === "SUCCEEDED" ? "succeeded" : "ended: " + run.status}. See `);
            status.append(a, " (published a minute or so after the run).");
            return;
          }
          if (run) say(`${sampleId}: uploaded; the run is ${run.status.toLowerCase()}.`);
        } catch (_) {
          // keep following; the status is a convenience
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
    cancel.addEventListener("click", () => controller && controller.abort());
    addEventListener("beforeunload", (e) => {
      if (busy) e.preventDefault(); // the browser asks before leaving; the upload can be resumed anyway
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (busy) return;
      const sampleId = $("up-id").value.trim(), chosen = files();
      const problem =
        (!$("up-token").value.trim() && "Enter the upload token.") ||
        checkSampleId(sampleId) ||
        (chosen.length !== (layout() === "paired" ? 2 : 1) && "Choose the file (both files for paired-end).") ||
        (chosen.some((f) => !/\.(fastq|fq)\.gz$/i.test(f.name)) && "Files must be gzipped FASTQ (.fastq.gz or .fq.gz).");
      if (problem) return say(problem, "error");
      for (const f of chosen) {
        const magic = new Uint8Array(await f.slice(0, 2).arrayBuffer());
        if (magic[0] !== 0x1f || magic[1] !== 0x8b) return say(`${f.name} isn't gzip-compressed.`, "error");
      }
      const entry = saved().find((s) => s.sample_id === sampleId);
      const resuming = entry && entry.files.length === chosen.length && entry.files.every((s, i) => sameFile(chosen[i], s));
      if (entry && !resuming) {
        return say(`${sampleId} has an unfinished upload of other files: choose those to carry on, or discard it.`, "error");
      }
      if (!resuming && existing.has(sampleId) && confirmedReplace !== sampleId) {
        confirmedReplace = sampleId;
        return say(`${sampleId} is already on the site: uploading replaces its reads and runs it again. Press Upload again to go ahead.`, "error");
      }

      busy = true;
      submit.disabled = true;
      cancel.hidden = false;
      controller = new AbortController();
      const run = io();
      const total = chosen.reduce((a, f) => a + f.size, 0), sentBy = chosen.map(() => 0), began = Date.now();
      const progress = (i) => (bytes) => {
        sentBy[i] = bytes;
        const sum = sentBy.reduce((a, b) => a + b, 0), secs = (Date.now() - began) / 1000;
        bar.value = total ? sum / total : 0;
        const rate = secs > 2 ? sum / secs : 0;
        const left = rate ? Math.round((total - sum) / rate / 60) : null;
        say(`Uploading ${sampleId}: ${(100 * bar.value).toFixed(0)}%` + (rate ? ` · ${(rate / 1048576).toFixed(1)} MB/s` : "") + (left !== null ? ` · about ${left} min left` : ""));
      };
      let started = resuming; // an upload exists to carry on with (then a failure can say its parts are kept)
      try {
        let plans;
        if (resuming) {
          plans = [];
          for (const [i, s] of entry.files.entries()) plans.push(await partsDone(run, chosen[i], s));
        } else {
          const metadata = {};
          if ($("up-gate").value) metadata["gate-profile"] = $("up-gate").value;
          if ($("up-reference").value) metadata.reference = $("up-reference").value;
          const begun = await run.api("/uploads", {
            sample_id: sampleId,
            layout: layout(),
            sizes: chosen.map((f) => f.size),
            metadata,
          });
          const at = Date.now() + begun.expires_in * 1000;
          plans = begun.uploads.map((u) => ({ upload: { ...u, expires_at: at }, done: new Map() }));
          started = true;
          localStorage.setItem(
            STORE + sampleId,
            JSON.stringify({
              sample_id: sampleId,
              created: new Date().toISOString(),
              files: begun.uploads.map((u, i) => ({
                key: u.key,
                upload_id: u.upload_id,
                part_size: u.part_size,
                name: chosen[i].name,
                size: chosen[i].size,
                lastModified: chosen[i].lastModified,
              })),
            }),
          );
          showPending();
        }
        for (const [i, plan] of plans.entries()) {
          await sendFile(run, chosen[i], plan.upload, plan.done, (bytes) => progress(i)(bytes));
        }
        localStorage.removeItem(STORE + sampleId);
        showPending();
        bar.value = 1;
        say(`Uploaded ${sampleId}. The run starts in a few seconds; this page follows it.`, "ok");
        follow(sampleId);
      } catch (err) {
        if (controller.signal.aborted) {
          say("Cancelled. The parts sent so far are kept: choose the same files to carry on, or discard the upload.");
        } else if (resuming && err.status === 404) {
          localStorage.removeItem(STORE + sampleId);
          say("That unfinished upload is gone (finished, discarded or expired). Press Upload to start again.", "error");
        } else if (started) {
          say(`${err.message} The parts sent so far are kept: press Upload again to carry on.`, "error");
        } else {
          say(err.message, "error");
        }
        showPending();
      } finally {
        busy = false;
        submit.disabled = false;
        cancel.hidden = true;
      }
    });

    showPending();
  };

  const exported = { ApiError, apiClient, checkSampleId, idFromName, partRanges, partsDone, sameFile, sendFile };
  globalThis.GeyserUpload = exported;
  if (typeof document !== "undefined" && document.getElementById("upload-form")) {
    if (document.prerendering) document.addEventListener("prerenderingchange", () => mount(document), { once: true });
    else mount(document);
  }
})();
