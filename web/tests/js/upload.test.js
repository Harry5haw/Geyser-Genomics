// node --test web/tests/js/   (Node 20+, no dependencies): the upload page's core (static/samples/upload.js).
// The file is a browser script; it runs here in a vm context without a document, so only the core is exported.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = path.join(__dirname, "..", "..", "samples", "static", "samples", "upload.js");
const context = vm.createContext({ setTimeout, clearTimeout, Promise, Map, Set, JSON, Math, Date, URL });
vm.runInContext(fs.readFileSync(SOURCE, "utf8"), context);
const U = context.GeyserUpload;

const MiB = 1024 * 1024;
const fakeFile = (size) => ({ size, slice: (start, end) => ({ start, end, size: end - start }) });

// A scripted web API + S3: records calls; `fail` maps part_number -> list of errors to throw first.
const harness = ({ fail = {}, expiresIn = 3600, now = 1_000_000, done = [] } = {}) => {
  const calls = [];
  const puts = [];
  const sleeps = [];
  let clock = now;
  const io = {
    api: async (route, body) => {
      calls.push({ route, body: JSON.parse(JSON.stringify(body)) });
      if (route === "/uploads/parts") {
        return {
          done,
          parts: body.part_numbers.map((n) => ({ part_number: n, url: `fresh-${n}` })),
          expires_in: expiresIn,
        };
      }
      return {};
    },
    put: async (url, blob, onBytes) => {
      const n = Number(url.split("-").pop());
      puts.push({ url, n, bytes: blob.size });
      const queued = fail[n];
      if (queued && queued.length) throw queued.shift();
      onBytes(blob.size / 2);
      return `"etag-${n}"`;
    },
    sleep: async (ms) => {
      sleeps.push(ms);
      clock += ms;
    },
    now: () => clock,
    online: async () => {},
    signal: { aborted: false },
  };
  return { io, calls, puts, sleeps };
};

const upload = (size, partSize, at = 1_000_000 + 3600 * 1000) => ({
  key: "raw_reads/S1.fastq.gz",
  upload_id: "U1",
  part_size: partSize,
  expires_at: at,
  parts: U.partRanges(size, partSize).map((r) => ({ part_number: r.part_number, url: `first-${r.part_number}` })),
});

test("sample IDs: the API's rule, and the ID a file name suggests", () => {
  assert.equal(U.checkSampleId("HG003-30x_chr20"), "");
  assert.match(U.checkSampleId(""), /1-30/);
  assert.match(U.checkSampleId("-x"), /starting/);
  assert.match(U.checkSampleId("a".repeat(31)), /1-30/);
  assert.match(U.checkSampleId("S1_R1"), /_R1 or _R2/);
  assert.equal(U.idFromName("HG003_R1.fastq.gz"), "HG003");
  assert.equal(U.idFromName("my reads.fq.gz"), "my-reads");
  assert.equal(U.checkSampleId(U.idFromName("x".repeat(40) + ".fastq.gz")), "");
});

test("parts cover the file exactly, the last one short", () => {
  const ranges = U.partRanges(10 * MiB + 1, 5 * MiB);
  assert.deepEqual(
    Array.from(ranges, (r) => [r.part_number, r.end - r.start]), // into this realm's arrays (vm context)
    [
      [1, 5 * MiB],
      [2, 5 * MiB],
      [3, 1],
    ],
  );
  assert.equal(U.partRanges(0, 5 * MiB).length, 0);
});

test("every part is sent once and the upload completed with each part's ETag, in order", async () => {
  const { io, calls, puts } = harness();
  const progress = [];
  await U.sendFile(io, fakeFile(12 * MiB), upload(12 * MiB, 5 * MiB), new Map(), (sent, total) =>
    progress.push([sent, total]),
  );
  assert.deepEqual(puts.map((p) => p.n).sort(), [1, 2, 3]);
  assert.deepEqual(calls.map((c) => c.route), ["/uploads/complete"]); // the URLs it was given were fresh enough
  assert.deepEqual(calls[0].body, {
    key: "raw_reads/S1.fastq.gz",
    upload_id: "U1",
    parts: [1, 2, 3].map((n) => ({ part_number: n, etag: `"etag-${n}"` })),
  });
  assert.deepEqual(progress.at(-1), [12 * MiB, 12 * MiB]);
});

test("a failing part is retried with growing waits, then the upload completes", async () => {
  const flaky = { 2: [Object.assign(new Error("reset"), { status: 0 }), Object.assign(new Error("503"), { status: 503 })] };
  const { io, calls, puts, sleeps } = harness({ fail: flaky });
  await U.sendFile(io, fakeFile(15 * MiB), upload(15 * MiB, 5 * MiB), new Map(), () => {});
  assert.equal(puts.filter((p) => p.n === 2).length, 3);
  assert.deepEqual(sleeps, [1000, 2000]);
  assert.equal(calls.at(-1).route, "/uploads/complete");
});

test("a 403 (an expired URL) renews that part's URL before trying again", async () => {
  const { io, calls, puts } = harness({ fail: { 1: [Object.assign(new Error("403"), { status: 403 })] } });
  await U.sendFile(io, fakeFile(5 * MiB), upload(5 * MiB, 5 * MiB), new Map(), () => {});
  assert.deepEqual(calls.map((c) => c.route), ["/uploads/parts", "/uploads/complete"]);
  assert.deepEqual(calls[0].body.part_numbers, [1]);
  assert.deepEqual(puts.map((p) => p.url), ["first-1", "fresh-1"]);
});

test("URLs about to expire are renewed first, once for all the parts still to send", async () => {
  const { io, calls, puts } = harness();
  const soon = 1_000_000 + 60 * 1000; // a minute left: inside the five-minute margin
  await U.sendFile(io, fakeFile(15 * MiB), upload(15 * MiB, 5 * MiB, soon), new Map(), () => {});
  assert.equal(calls.filter((c) => c.route === "/uploads/parts").length, 1);
  assert.deepEqual(calls[0].body.part_numbers, [1, 2, 3]);
  assert.ok(puts.every((p) => p.url.startsWith("fresh-")));
});

test("a part that keeps failing gives up after six tries and doesn't complete the upload", async () => {
  const always = { 1: Array.from({ length: 10 }, () => Object.assign(new Error("down"), { status: 500 })) };
  const { io, calls, puts } = harness({ fail: always });
  await assert.rejects(U.sendFile(io, fakeFile(5 * MiB), upload(5 * MiB, 5 * MiB), new Map(), () => {}), /down/);
  assert.equal(puts.length, 6);
  assert.ok(!calls.some((c) => c.route === "/uploads/complete"));
});

test("a cancelled upload stops before sending anything more and doesn't complete", async () => {
  const { io, calls, puts } = harness();
  io.signal.aborted = true;
  await assert.rejects(U.sendFile(io, fakeFile(10 * MiB), upload(10 * MiB, 5 * MiB), new Map(), () => {}), /cancelled/);
  assert.equal(puts.length, 0);
  assert.equal(calls.length, 0);
});

test("resuming sends only the parts S3 doesn't have at the right size", async () => {
  const done = [
    { part_number: 1, etag: '"old-1"', size: 5 * MiB },
    { part_number: 2, etag: '"short-2"', size: 1234 }, // an interrupted part: not done
  ];
  const { io, calls, puts } = harness({ done });
  const saved = { key: "raw_reads/S1.fastq.gz", upload_id: "U1", part_size: 5 * MiB };
  const plan = await U.partsDone(io, fakeFile(12 * MiB), saved);
  assert.deepEqual([...plan.done.keys()], [1]);
  assert.deepEqual(calls[0].body.part_numbers, []); // progress only: URLs come as the parts are sent
  await U.sendFile(io, fakeFile(12 * MiB), plan.upload, plan.done, () => {});
  assert.deepEqual(puts.map((p) => p.n).sort(), [2, 3]);
  assert.deepEqual(calls[1].body.part_numbers, [2, 3]); // one renewal for both, not one each
  const complete = calls.at(-1).body.parts;
  assert.deepEqual(complete.map((p) => p.etag), ['"old-1"', '"etag-2"', '"etag-3"']);
});

test("a big file's URLs come at most 1,000 at a time (the API's limit), as the parts get to them", async () => {
  const { io, calls, puts } = harness();
  const parts = 2500;
  const noUrls = { key: "raw_reads/S1.fastq.gz", upload_id: "U1", part_size: 5 * MiB, parts: [], expires_at: 0 };
  await U.sendFile(io, fakeFile(parts * 5 * MiB), noUrls, new Map(), () => {});
  const renewals = calls.filter((c) => c.route === "/uploads/parts").map((c) => c.body.part_numbers);
  assert.deepEqual(renewals.map((r) => [r[0], r.length]), [[1, 1000], [1001, 1000], [2001, 500]]);
  assert.equal(new Set(puts.map((p) => p.n)).size, parts);
  assert.equal(calls.at(-1).body.parts.length, parts);
});

test("the same file is recognised by name, size and date", () => {
  const saved = { name: "a.fastq.gz", size: 10, lastModified: 5 };
  assert.ok(U.sameFile({ name: "a.fastq.gz", size: 10, lastModified: 5 }, saved));
  assert.ok(!U.sameFile({ name: "a.fastq.gz", size: 11, lastModified: 5 }, saved));
});

test("the API client sends the token as a bearer header and reports the API's message", async () => {
  const seen = [];
  const fetchOk = async (url, init) => {
    seen.push({ url, init });
    return { ok: true, status: 200, json: async () => ({ fine: true }) };
  };
  const call = U.apiClient("https://api.test", "t0k3n", fetchOk);
  assert.equal((await call("/uploads", { a: 1 })).fine, true);
  assert.equal(seen[0].url, "https://api.test/uploads");
  assert.equal(seen[0].init.headers.Authorization, "Bearer t0k3n");
  assert.equal(seen[0].init.body, '{"a":1}');

  const refused = U.apiClient("https://api.test", "bad", async () => ({
    ok: false,
    status: 401,
    json: async () => ({ error: "the upload token is not valid" }),
  }));
  await assert.rejects(refused("/uploads", {}), (err) => err.status === 401 && /not valid/.test(err.message));
  const offline = U.apiClient("https://api.test", "t", async () => {
    throw new TypeError("Failed to fetch");
  });
  await assert.rejects(offline("/uploads", {}), (err) => err.status === 0 && /can't be reached/.test(err.message));
});

test("sendParts sends the parts but leaves completing to the caller (a pair's mates complete together)", async () => {
  const { io, calls, puts } = harness();
  const parts = await U.sendParts(io, fakeFile(10 * MiB), upload(10 * MiB, 5 * MiB), new Map(), () => {});
  assert.equal(puts.length, 2);
  assert.equal(calls.length, 0);
  assert.deepEqual(Array.from(parts, (p) => p.etag), ['"etag-1"', '"etag-2"']);
});

test("an error retrying can't fix (the upload is gone) isn't retried and stops the other workers", async () => {
  const gone = { 1: [Object.assign(new Error("no such upload"), { status: 404 })] };
  const { io, calls, puts, sleeps } = harness({ fail: gone });
  await assert.rejects(U.sendParts(io, fakeFile(40 * MiB), upload(40 * MiB, 5 * MiB), new Map(), () => {}), /no such/);
  assert.equal(sleeps.length, 0);
  assert.ok(puts.length <= 4, `${puts.length} parts sent after the failure`); // only the first batch in flight
  assert.equal(calls.length, 0);
});

test("a failed URL renewal is retried like a failed part", async () => {
  const { io, calls, puts, sleeps } = harness();
  const api = io.api;
  let failures = 1;
  io.api = async (route, body) => {
    if (route === "/uploads/parts" && failures-- > 0) throw Object.assign(new Error("503"), { status: 503 });
    return api(route, body);
  };
  const noUrls = { key: "raw_reads/S1.fastq.gz", upload_id: "U1", part_size: 5 * MiB, parts: [], expires_at: 0 };
  await U.sendFile(io, fakeFile(5 * MiB), noUrls, new Map(), () => {});
  assert.deepEqual(sleeps, [1000]);
  assert.equal(puts.length, 1);
  assert.equal(calls.at(-1).route, "/uploads/complete");
});

test("the status after an upload is that upload's run, not an earlier run of the sample", () => {
  const since = Date.parse("2026-11-04T12:00:00Z");
  assert.ok(U.runOfUpload({ started: "2026-11-04T12:00:05+00:00" }, since));
  assert.ok(U.runOfUpload({ started: "2026-11-04T11:59:30+00:00" }, since)); // within the clock allowance
  assert.ok(!U.runOfUpload({ started: "2026-11-04T09:00:00+00:00" }, since));
  assert.ok(!U.runOfUpload({ status: "NOT_FOUND" }, since));
});

test("an S3 400 (RequestTimeout, ExpiredToken) renews the URL and retries; an API 400 doesn't", async () => {
  const stalled = { 1: [new U.ApiError(400, "RequestTimeout")] };
  const { io, calls, puts } = harness({ fail: stalled });
  await U.sendFile(io, fakeFile(5 * MiB), upload(5 * MiB, 5 * MiB), new Map(), () => {});
  assert.deepEqual(puts.map((p) => p.url), ["first-1", "fresh-1"]);
  assert.equal(calls.at(-1).route, "/uploads/complete");

  const refused = harness();
  refused.io.api = async () => {
    throw new U.ApiError(400, "part_numbers must be whole numbers", true);
  };
  const noUrls = { key: "raw_reads/S1.fastq.gz", upload_id: "U1", part_size: 5 * MiB, parts: [], expires_at: 0 };
  await assert.rejects(U.sendParts(refused.io, fakeFile(5 * MiB), noUrls, new Map(), () => {}), /whole numbers/);
  assert.equal(refused.sleeps.length, 0);
});
