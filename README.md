# HueMem — Self-describing PNG memory for AI agents

Self-describing PNG memory. A memory is a PNG: the words, their
categories, and the checks to recover them live in the pixels, not
in a sidecar file that can be lost or stripped.

This public release contains only the capability — the code, the
spec, and the verify format. It contains no memories. Create your
own memories locally with the code below; they stay yours.

## What it is

Text → grammar categories → RGB → PNG.

- Hue = category (what kind of thing a word is)
- Position left → right = word order
- Dominant hue = the phase of the memory

Palette:

- PERSON (255, 99, 71), GPE (46, 139, 87), LOC (60, 179, 113)
- ORG (70, 130, 180), PRODUCT (218, 165, 32)
- NOUN (100, 149, 237), PROPN (65, 105, 225)
- VERB (255, 165, 0), ADJ (186, 85, 211), ADV (221, 160, 221)
- OTHER (169, 169, 169)

## Recovery contract

- Header: 48 bytes in the first 16 RGB pixels, magic `HUEM`, version 1.
- The full ordered word list is encoded in pixels after the header.
  A moved PNG answers its id, hashes, dominant category, counts,
  and word list without its `.md`.
- Filename: `HUEMv1_<DOM>_<TICK>_r<XX>_<HASH8>_<slug>.png`
- One memory = one directory: `memories/<slug>-<hash8>/`
- Memories over 256 words split in order into children of at most
  256 words. The parent carries a child manifest plus XOR parity
  of the child word blocks. A missing child can be regrown from
  the parent parity plus the surviving siblings; a destroyed
  parent can be rebuilt from the children.
- Verify is on-demand only. No watcher, no autonomous regrowth.

## Accepted limits

- Cropping the header row loses the binary block.
- A single-child split has no parity gain.
- Detection is manual, via verify.

## Use it

```bash
pip install Pillow
# optional, for full tagging accuracy:
# pip install spacy
# python -m spacy download en_core_web_sm
```

Create a memory in Python:

```python
from huemem import write_standalone, write_split

write_standalone("Ada Lovelace wrote an early algorithm in London.",
                 slug="ada-sample", title="Ada sample", root="memories")
```

Verify it:

```bash
python3 huemem.py verify memories/ada-sample-<hash8>
```

Verify reports three checks: manifest child count vs files
present, per-child CRC32 match / mismatch, and parent parity
CRC match when 2+ children exist. Overall: PASS or FAIL, with
missing and mismatched lists. FAIL does not trigger regrowth —
regrowth is a separate manual command.

The v1 writer is in `text_to_png.py`. It tries spaCy first
and falls back to a small example tagger plus heuristics.

Full spec: `V2_SPEC.md`. Shared layout, writer, reader,
verify, regrow, and rebuild-parent all live in `huemem.py` —
no duplicated offset math. PNGs are written atomically:
temp file, fsync, rename.

## Recall

The PNG is the authoritative record; the recall index is derived and
regenerable. `huemem_recall.py` builds it on demand and retrieves with
a confidence-gated switch: a one-sentence plain-language summary per
memory (a `plain:` line in the memory's `.md` sidecar, written in the
plainest everyday words) is scored by dense cosine, and when its top-1
score is ≥ 0.40 with a margin ≥ 0.05 over the runner-up, the plain
ranker's top-1 is the answer. Otherwise the answer comes from
three-way reciprocal-rank fusion (k=60) over lexical IDF overlap with
synonym expansion, dense cosine over the memory word list, and dense
cosine over a one-sentence distinguishing description written at write
time (a `distinguishing:` line in the sidecar; the `source:` sentence
is used when it is absent).

```bash
python3 huemem_recall.py search memories "Who wrote an early algorithm?" --top 3
python3 huemem_recall.py search memories "Why is the connection blocked?" --abstain
```

Measured results on a frozen 40-memory test set, including the
head-to-head against plain text search and the audit caveats, are in
`RESULTS.md`. Headline: paraphrase rank-1 39/43 against 14/43 for plain
text search on the same prompts; Recall@3 42/43, Recall@5 43/43;
two-turn 9/9; exact 10/10; vocabulary-gap 6/6. Honest expectation
~38/43 after split-half validation. The plain summaries are
human-authored — a small local generator writing them collapses the
system to 34/43, so that number is a human-summary number.

Abstention is not solved in the strong sense: no point gives both
zero false refusals and full no-memory abstention. The shipped
operating point is a score-distribution logistic
(`--abstain-logistic`, weights in `abstain_logistic_weights.json`):
honest leave-one-out 8/11 no-memory prompts abstained at zero false
refusals, 1/43 false refusals at full abstention.

The accelerator library (`huemem_accel.py`, documented in
`ACCELERATOR.md`) is the fast path over the same system: a
streaming header-only PNG reader, a derived index, vectorized search
that agrees with the original on all 53 test prompts, and a read-only
parity fault report. At N = 10,000 memories a query runs about
100–200 ms on the test VM; collisions, not latency, are what degrades
first.

## License

MIT. See `LICENSE`.
