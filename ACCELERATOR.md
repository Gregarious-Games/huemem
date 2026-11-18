# HueMem Accelerator

The accelerator library (`huemem_accel.py`) and the measured answers
to the three skeptic questions about HueMem: why a PNG instead of a
database, the exact failure threshold of the parity math, and what
happens at N = 10,000.

Audience: a systems engineer who is skeptical of the PNG container,
the parity math, and the 40-memory test set — in that order. The stance
throughout is: concede the costs first, then show the trade. No number
is quoted without its set size beside it, and no claim is made that
the measurements do not support.

Measured on the frozen 40-memory test set (RESULTS.md): the best recall
system is the confidence-gated switch H — paraphrase rank-1 39/43,
Recall@3 42/43, Recall@5 43/43, two-turn 9/9, exact 10/10,
vocabulary-gap 6/6, honest expectation ~38/43 after split-half
validation. The 4 remaining misses are all near-neighbour collisions,
ranked 2nd–4th. That set is the baseline for everything below; it is a
start, not a scaling proof, and Section 3 says so plainly.

Verification status (all measured 2026-11-18, `accel_test.py`,
exit 0): CADE→HUEM conversion 40/40 word-identical; header fast path
40/40 fields match; header scan 0.15 ms/PNG, 241x faster than a full
load; `AccelIndex.search` top-1 agrees with `RecallIndex.search` on
all 53 prompts (0 diffs), scoring paraphrase 39/43, exact 10/10,
two-turn t1 9/9 t2 8/9 — the t2 8/9 is the public module's generic
synonym map, not the accelerator: the original scores 8/9 on the same
map, where the private harness's corpus-specific map scores 9/9. Fault
boundary 9/9 cases pass; the eight checks for the review fixes in
Section 2 pass 8/8.

---

## 1. Why PNG instead of SQLite / Parquet / Flatbuffers?

The skepticism is fair, so take it in full first. A PNG adds zlib
compression overhead to every read and write. It needs an image parser
just to reach a 48-byte header. It is not appendable: you cannot add a
record to a PNG, you write a new file. A SQLite database, a Parquet
file, or a Flatbuffers blob would be faster for random access, smaller
on disk, and natively queryable. If the only job were "store structured
records and query them fast," the PNG would lose, and there would be
nothing interesting to say about it.

The answer is that the PNG is not doing the query job. HueMem splits
the system in two, and the split is the whole argument:

- **The PNG is the authoritative archive.** One memory, one file (or
  one small group of files for a long memory). It carries the identity,
  the category, the hashes, and the full ordered word list, and it
  carries them in a form that survives without any HueMem code at all.
- **The derived index is the query-time structure.** `build_index`
  reads the PNGs once and produces `index.json` (token postings,
  document frequencies, metadata) plus one float32 embedding matrix
  per leg (`emb_word.npy`, `emb_dist.npy`, `emb_plain.npy`). That index
  is exactly the SQLite/Parquet-shaped thing — a regenerable,
  decoupled structure that query time lives in. Deleting it loses
  nothing; rebuild it from the PNGs.

So the question is not "PNG or database." It is "what should the
archive be, given that the query path is already a derived index?"
And the archive's job is durability, inspectability, and portability
across systems that do not run HueMem code. That is where the image
container earns its keep:

- **Zero-dependency inspection.** Any image viewer on any phone, laptop,
  or edge device opens a PNG. No database engine, no HueMem install,
  no tooling. A memory can be read by a human holding the file, and
  its header can be read by anything that can parse the first row of
  pixels. On the deployment HueMem is built for — an agent living on a
  phone — that matters: the file is legible to the device itself, not
  only to the application.
- **Native OS media-store compatibility.** The gallery is the shared
  substrate. A PNG is indexed by the OS media store, shows up in the
  gallery, and rides along with the backup and sync pipelines the
  device already has. A SQLite file gets none of that: it is invisible
  to the gallery, unbacked by photo backup, and unfindable by the
  operating system's own search. HueMem does not build a sync layer,
  a backup layer, or a viewer — it inherits all three from the
  platform, because the container is a picture.
- **Visual corruption diagnostics.** A damaged PNG shows up: the image
  is visibly wrong, or it fails to open at all. A truncated SQLite
  file or a corrupt Parquet footer fails opaquely, often only when the
  exact damaged page is touched. The PNG's visual bars are a
  human-readable summary of the memory's category mix, and the header
  carries a CRC32 over the payload, so silent corruption is caught by
  `verify` and visible corruption is caught by looking. Both failure
  modes surface early instead of at query time.
- **Immutability fits the memory model.** Memories are not edited in
  place; they are written once, and a correction is a new memory. The
  PNG's non-appendability is therefore not a limitation to work
  around — it matches the write pattern. Append means "write another
  file," which is also how the archive stays shardable: one memory's
  failure never touches another's.

None of this denies the costs. The costs are real, and the accelerator
pays them down rather than hand-waving them:

- The header-behind-a-parser cost is paid by `read_header_fast`,
  which decompresses only the first row of the PNG instead of decoding
  the whole image. Measured: 40/40 header fields match the full decode
  on the golden set, and an independent review verified the row-0
  unfiltering for all five PNG filter types (12/12 synthetic checks:
  filters 0–4, split IDAT, ancillary chunks, non-HUEM / truncated /
  empty files correctly rejected). Two precision notes, stated
  plainly: the file itself is read fully into memory — only the
  decompression is partial — and the fast path does not check chunk
  CRCs, so it is a header scan, not an integrity check. Integrity is
  `read_png` / `verify_dir`'s job.
- The scan cost is paid by `scan_headers`, which sweeps a directory of
  headers without touching a payload. Measured on the golden set:
  40 PNGs in 0.006 s (0.15 ms per PNG) against 1.466 s for a full load
  — 241x faster. On 200 small synthetic PNGs the same sweep ran
  0.06 ms/PNG, 323x faster than a full load. That is what makes a
  directory sweep cheap, and it is what the scaling argument in
  Section 3 stands on.

The honest summary: if you want the fastest possible query engine and
nothing else, use a database. If you want an archive that any device
can read, that the OS already indexes, backs up, and syncs, and that a
human can inspect with no tooling — and you are willing to keep the
fast query path in a derived index, which is what HueMem does — the
PNG earns its keep.

---

## 2. What is the exact failure threshold of the parity math?

One parity equation cannot solve for two unknowns. That sentence is
the whole boundary, and everything else in this section is working out
what it means for each shape of memory.

A split memory is a group: one parent PNG carrying a manifest and a
single XOR parity block (the XOR of the child word blocks, zero-padded
to the longest child), plus N child PNGs carrying the word blocks. The
parity is per group, not global. It is RAID-5 style: one redundant
member per group, tolerating exactly one erasure per group.

| Erasures in one split group | Outcome | Recovery action |
|---|---|---|
| 0 | Intact | None |
| 1 — the parent | Recoverable | `rebuild_parent`: re-derive the manifest and parity from the intact children |
| 1 — exactly one child | Recoverable | `regrow_missing`: rebuild the child from the parent parity XOR the surviving siblings |
| 2 — two children | **Unrecoverable from parity** | None: one equation, two unknowns |
| 2 — parent + one child | **Unrecoverable from parity** | None: the parity block lives in the parent, so both the equation and one unknown are gone |
| ≥3 | **Unrecoverable from parity** | None |

Verified: all nine fault cases pass in `accel_test.py` (intact split,
one child lost, parent lost, two children lost, parent + child lost,
standalone intact / PNG lost / PNG corrupt / PNG + sidecar lost).

Three consequences worth stating plainly, because they are where
systems engineers will probe:

- **Erasures in different groups are independent.** Losing one child
  in group A and one child in group B is two single erasures, each
  recoverable — the same way losing one disk in each of two RAID-5
  arrays is fine. The threshold is per group, not per corpus.
- **A corrupt parent counts as a parent erasure.** If the parent's
  payload fails its CRC, its manifest and parity are untrusted, so the
  group is treated as "parent lost." Intact children can still be
  re-parented — `rebuild_parent` reads the children, not the parent —
  but a child that is also missing stays lost, because there is no
  trusted parity to regrow it from. When the parent is untrusted, the
  child list is enumerated from the files on disk, not from the
  manifest: a corrupt manifest byte once made healthy children look
  missing (a false "unrecoverable"), and the fix is to trust the disk,
  not the manifest. A deleted parent is the same as a corrupt one —
  and a missing child is detected even without the manifest, by a gap
  in the child-number sequence in the filenames.
- **A single-child split has no parity gain.** With one child there is
  nothing to XOR against; the parent is a manifest only. That is a
  known limit, stated in the v2 spec, not a surprise. Long memories
  (the ones that split) almost always have several children, which is
  where the parity does its work.

Two honest limits on the detection itself:

- A missing **highest-numbered** child is undetectable when the parent
  is gone: the surviving child numbers still run 1..N with no gap, and
  nothing else records the true total. The report says this wherever
  it rebuilds without a manifest, so a rebuild from surviving children
  never claims to have confirmed that none are missing.
- The fault story itself was buggy before this document: the code
  located the parent by checking whether the filename *contained*
  "-parent", so any memory whose slug contains the word "parent"
  broke verification outright — an intact memory reported as
  unrecoverable. The same class of bug existed for "-child" in the
  rebuild path. Both are fixed (matched on the writer's actual
  suffixes) and covered by the eight review checks, which pass 8/8:
  parent-slug split intact, parent-slug verify PASS, truncated parent
  reports a rebuild instead of crashing, manifest corruption rebuilds
  from disk, split-aware loading returns the full 1,800-word list
  (not one child's fragment), a 1-memory search no longer crashes,
  and a rebuild with 12 children preserves word order (3,000/3,000).

A standalone memory (one PNG, no split) has no parity at all. Its
recovery path is different and worth naming separately: the `.md`
sidecar carries the full source text, and the stable id is derived
from slug + source, so a lost or corrupt standalone PNG can be
rewritten from the sidecar, identically except for the tick. One
caveat: "identical" holds only if the rewrite uses the same tagging
path (the fallback regex and spaCy tokenize differently, e.g. on
contractions) and the same title. If the PNG and the sidecar are both
gone, the memory is lost — there is no third copy, and the document
does not pretend otherwise.

`fault_report` (in `huemem_accel.py`) states this boundary per memory,
read-only. It returns the kind (standalone / split), the list of
erasures, whether the group is recoverable, and the named recovery
action — `none`, `regrow_missing`, `rebuild_parent`, or
`rewrite_from_sidecar` — plus the missing child's manifest index when
the action is `regrow_missing`. It never performs the recovery: regrow
and rebuild require explicit `confirm=True` in `huemem.py`, by design,
and `regrow_missing` now CRC-checks the surviving siblings and
verifies the recovered chunk against the manifest's source hash before
writing, so a silently corrupt survivor cannot produce a wrong child
that passes its own verification. Naming the action without taking it
is the point — the operator decides, the tool reports.

What the parity math does **not** do: it is not double parity. RAID-6
survives two erasures per group; HueMem does not implement that, and
the reason is cost against group size — a second parity block doubles
the parity storage and the rebuild arithmetic for groups that are
usually two to seven children. If groups get much larger, double
parity becomes the right trade, and it is a defined future option, not
a redesign.

---

## 3. What happens at N = 10,000?

Start with the honest sentence: 43 prompts on 40 memories is a start,
not a scaling proof. At that size, brute-force header scans and hybrid
scoring take milliseconds, and nothing about the measurement strains
the system. The question is what breaks first as N grows — latency,
memory, or correctness — and the answer is not the same for all three.

### The architecture answer: query time never touches a PNG

The derived index is decoupled from the authoritative PNGs. A query
runs against `index.json` plus the three embedding matrices, all
already in memory. The PNGs are read once, at build time, and again
only for verification or recovery. Adding a memory appends one row to
each matrix and a posting-list entry per token; a full rebuild is the
simple path and its cost is measured below. Deleting the index loses
nothing.

### The cost model, per recall pass

- **Lexical leg:** a posting-list lookup per query token. Cost grows
  with the query's vocabulary and the average posting-list length,
  which grows sublinearly with N (vocabulary saturates; new memories
  mostly reuse existing tokens).
- **Dense legs:** one BLAS matrix multiply per leg — the whole
  embedding matrix against the query vector. Cost is linear in N and
  fully vectorized; there is no per-memory Python loop in the hot
  path.
- **Fusion and gate:** the RRF is a sort over N scores, and the gate
  is a comparison on the top two plain-cos scores. Both are negligible
  next to the matmul at any N in this range.
- **Query encoding:** one forward pass of the encoder per query. It
  does not scale with N at all — it is a constant, measured in the
  benchmark.

### Measured: per-query latency (`accel_bench.py` Part A)

Full search path (lexical posting lookup + one matmul per leg + RRF +
gate), 50 random queries, synthetic word lists, random unit vectors
(matmul cost is content-independent). The primary run uses 768-dim
vectors, matching the shipped MPNet model; a second run at 384 dims
read higher at large N, and the difference is this box's throttle
state, not the dimension — the 768 run is the one to quote, with the
384 run as the throttle-variance bound.

| N | mean (768-dim) | p95 (768-dim) | mean (384-dim, throttled run) |
|---|---|---|---|
| 100 | 0.84 ms | 1.27 ms | 0.48 ms |
| 1,000 | 7.74 ms | 8.99 ms | 7.72 ms |
| 5,000 | 49.83 ms | 64.23 ms | 108.68 ms |
| 10,000 | 110.16 ms | 132.02 ms | 205.08 ms |

Read it as: **order of 100–200 ms per query at N = 10,000 on this
throttled VM**, plus the query-encode constant below. The curve is
roughly linear from 1,000 up (7.7 → 50 → 110 ms for 1k → 5k → 10k),
so brute-force search does not fall off a cliff at 10,000 — it gets
proportionally slower, and stays under a quarter of a second. The
honest bound is the range, not a single number: this box's CPU is
heavily throttled and its speed moves the large-N figure by about 2x
between runs.

### Measured: build cost (`accel_bench.py` Part B)

- **MPNet encoding: 2.1 docs/s on this box.** Three legs per memory
  means a full build at N = 10,000 costs ≈ 14,465 s, about 4 hours.
  A 200-memory `build_index` took 265.8 s, consistent with that rate.
  This is a statement about *this throttled VM*, not about the model:
  the same encode on unthrottled hardware would be far faster, and
  this document does not invent a faster number it did not measure.
  The build is also the part that parallelizes trivially — it is 30,000
  independent encodes — and it is a one-time cost, amortized over
  every query the index serves.
- **Header scan vs full load, 200 PNGs:** scan 0.01 s (0.06 ms/PNG);
  full load 3.58 s — 323x slower. (Small synthetic PNGs; the golden
  set reads 0.15 ms/PNG, 241x.)
- **Query encode: 50.6 ms**, constant, independent of N — again on
  this throttled box. A live query at N = 10,000 therefore costs
  roughly 160–255 ms end-to-end here (110–205 ms search + ~50 ms
  encode), still well under a second.

Two derived figures, computed from verified constants rather than
measured, to frame what the bench shows:

- **Index size:** 3 legs × N × 768 dimensions × 4 bytes (float32) =
  9,216 bytes per memory, so ~92 MB at N = 10,000. The embedding
  dimension (768, all-mpnet-base-v2) was read from the cached model
  config, not assumed. The token map and metadata are small next to
  the matrices.
- **Where brute force stops:** the dense cost is one matmul, so the
  line where it stops being fast enough is the line where N × 768
  exceeds the per-query latency budget on the target device. Past
  that line the index needs either sharding (per-category or per-shard
  matrices, queried selectively) or approximate nearest neighbours
  (HNSW / IVF over the same matrices). At N = 10,000 on this hardware
  the measured 110–205 ms does not force that decision — brute force
  still fits the budget. The claim to test at the next order of
  magnitude, not to assert now.

### The real scaling risk is correctness, not latency

Latency is linear and vectorized; collisions are the risk that grows
faster. At N = 40, the 4 remaining misses are all near-neighbour
collisions — the plain-language match is confident but points at a
neighbour, ranked 2nd–4th, with every expected memory in the top 5
(Recall@3 42/43, Recall@5 43/43). As N grows, neighbour density
grows: more memories share a topic, an entity, or a phrasing, and the
top-1 margin shrinks. That is the curve that matters, and the
benchmark measures it directly.

### Measured: collisions at N = 300 (`accel_bench.py` Part C)

300 synthetic memories from 8 topic templates with entity variations
(same topic, different name / number / place — the near-neighbour
structure that causes collisions), 100 queries made by
synonym-swapping and dropping words from each memory's plain sentence,
real MPNet embeddings, full gated search:

- **Top-1: 74/100.**
- **Low-margin (top-1 plain-cos margin < 0.05): 93/100** — the gate
  almost always falls back to fusion at this density.

Compare with the 40-memory baseline: 4/43 near-misses (~9%), all
ranked 2nd–4th. At N = 300 on confusable templates the miss rate is
26%, and the gate's confidence signal has nearly vanished — 93% of
queries cannot be decided by the plain ranker alone. Two caveats, both
load-bearing: this is a **stress test**, not a natural corpus — every
memory is a near-neighbour of several others by construction, which
is far denser than a real 300-memory archive — and it was measured at
**N = 300, not 1,000**, because a runtime restart killed the first
1,000-memory run and the box stays throttled; the run was shrunk and
checkpointed rather than risked again. Even with those caveats, the
direction is the finding: collisions, not latency, are what degrades
first, and the gate's margin is the first thing to go.

What already mitigates collisions, and what does not:

- The distinguishing sentence — one sentence per memory stating what
  makes it different from its neighbours — is the write-time
  disambiguator. It replaced the whole-.md cosine as a fusion leg
  (34/43 as a replacement vs 32/43 as a fourth ranker) precisely
  because it carries the difference, not the similarity.
- The confidence gate is the run-time disambiguator: when the plain
  ranker is unsure, the system falls back to the three-way fusion
  instead of trusting a confident-looking neighbour. At N = 300 on
  the stress templates it falls back 93% of the time — which is the
  gate working, not failing — but it means the fusion path, not the
  plain path, carries the accuracy at scale, and the fusion path's
  74/100 is the number to improve.
- The Round 27 fine-tuning direction (contrastive training on
  entity-swapped near-duplicates) showed a real paired +3 lift in
  cross-validation (37/43 tuned vs 34/43 base) but no system-level
  win — it is the direction that attacks collisions directly, and it
  needs a bigger corpus to show a win. That is a data-scale statement,
  and it is the same statement this section makes: at N = 10,000 the
  corpus is the lever, not another fusion variant.

### What N = 10,000 does not change

The archive does not change: one memory, one file (or one small
group), the same 48-byte header, the same verify. The derived index
does not change shape: the same JSON + matrices, larger. The search
does not change algorithm: the same gate, the same three-way RRF, the
same thresholds (0.40 / 0.05), unless the bench shows the gate point
drifting with N — in which case the gate is re-tuned on the larger
corpus and the re-tuning is reported, not silently applied. The
abstention story does not change either, except that the score
distribution the Round 24 logistic was fitted on (40 memories) must
be refitted on the larger corpus; the weights file says so, and the
document repeats it, because a threshold fitted at N = 40 is not a
threshold validated at N = 10,000.

---

## The accelerator library (what the code is)

`huemem_accel.py`, in the public HueMem repo (capability only, no
memories):

| Function | What it does |
|---|---|
| `read_header_fast(path)` | Decompresses only the first row of a PNG through zlib; returns the parsed 48-byte header plus stats (file size, compressed bytes consumed, filter type). Falls back to the full `read_png` on anything it cannot handle (interlaced, non-8-bit, unknown color type). |
| `scan_headers(mem_dir)` | Header-only sweep of every memory directory: identity and integrity, no payloads. Measured 0.15 ms/PNG, 241x faster than a full load. |
| `build_index(mem_dir, out_dir)` | One-time build of the derived index from the authoritative PNGs. Full PNG reads happen here and only here. Split memories are loaded split-aware: children concatenated in manifest order. |
| `AccelIndex.search(prompt, ...)` | Query-time search over the built index: posting-list lexical scores, one matmul per dense leg, three-way RRF (k=60), confidence gate (0.40 / 0.05). Mirrors `huemem_recall.py` exactly — same lex formula, same synonym map, same gate. Verified: top-1 agrees with the original on all 53 prompts (0 diffs). |
| `fault_report(memory_dir)` | Read-only statement of the parity boundary for one memory (Section 2). Names the recovery action; never performs it. Verified: 9/9 fault cases. |

No watcher, no autonomous writes. The index is rebuilt on demand from
the PNGs; deleting the index loses nothing.

Two decisions, recorded: the accelerator's search stays threshold-only
— the Round 24 abstention logistic lives in `huemem_recall.py` and is
not duplicated here (passing it to `AccelIndex.search` raises
`TypeError`, fail-loud by design). And split-aware loading is in scope
for the accelerator: a split memory is indexed as its full word list,
not silently as one child's fragment.

---

## Open questions and honesty notes

- The 39/43 is a human-authored-plain-sentence number. A small local
  generator writing the plain sentences collapses the gated system to
  34/43 (Round 20). Whether an automated writer can produce summaries
  as good as a human's is untested, and the scaling story in Section 3
  inherits that caveat: at N = 10,000, the plain sentences have to
  come from somewhere, and the corpus cannot be hand-written at that
  scale. That is the largest open question in the whole document, and
  it is not a footnote.
- The build and encode rates (2.1 docs/s, 50.6 ms per query encode)
  are this throttled VM's rates, not the model's. Do not quote them
  as hardware-independent. The latency curve has the same caveat in
  the other direction: the N = 10,000 figure moved ~2x between runs
  with the throttle state, so the document quotes the range, not a
  single number.
- The collision measurement is N = 300 on a synthetic stress corpus,
  not N = 1,000 on a natural one. The 74/100 and 93% low-margin
  figures say the gate leans on fusion at density; they do not say a
  real 300-memory archive scores 74/100.
- The abstention logistic's weights were fitted on 40 memories. They
  are the honest defaults for a small corpus, not a validated
  operating point at N = 10,000. Refit on labelled no-memory prompts
  at the target scale.
