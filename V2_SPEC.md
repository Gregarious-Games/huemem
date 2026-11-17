# HueMem v2 — First Recovery Primitive (accepted baseline 2026-11-17)

v1 measured baseline: PNG is pixels only, no embedded map, no checksum,
no tick, no structured word list. Filename is slug only.
Two blue-dominant memories indistinguishable without opening both.

## First primitive: header in first N pixels + filename invariants
No parent / parity yet. This primitive closes:
- "two blue memories indistinguishable"
- "PNG cannot answer without .md"

## Filename format
```
HUEMv1_<DOM>_<TICK>_<rXX>_<HASH8>_<slug>.png
```
Example:
```
HUEMv1_NOUN_20261117_r50_a1b2c3d4_ada-sample.png
```
- `HUEMv1` — schema prefix, palette v1, header v1
- `<DOM>` — dominant category code: PERSON, GPE, LOC, ORG, PRODUCT, NOUN, PROPN, VERB, ADJ, ADV, OTHER
- `<TICK>` — YYYYMMDD, creation date, e.g. 20261117
- `<rXX>` — residual intensity, dominant % 0-100, e.g. r50 = 50% NOUN
- `<HASH8>` — first 8 hex chars of SHA256(source text), matches header source hash
- `<slug>` — human slug, e.g. ada-sample

Index scan can answer dominant category, date, intensity, and identity
without opening the payload.

## Pixel header — exact byte layout, first 16 RGB pixels = 48 bytes
Header is stored as raw bytes in the first 16 pixels of row 0,
left to right, 3 bytes per pixel, big-endian. Visual bars start at
pixel 16, row 0, and below. Header row is 1 pixel high, visually
black except for header bytes, so it is distinguishable.

| Bytes | Field | Type | Example for ada-sample |
|-------|-------|------|----------------------|
| 0-3 | magic | ASCII "HUEM" | 43 41 44 45 |
| 4 | version | uint8, header v1 / palette v1 | 0x01 |
| 5 | dominant category | uint8 enum | 6 = NOUN |
| 6-7 | word count | uint16 BE | 12 |
| 8-11 | tick | uint32 BE, unix seconds | 179493... |
| 12-15 | title hash | uint32 BE, first 4 bytes SHA256(title) | — |
| 16-19 | source hash | uint32 BE, first 4 bytes SHA256(source text) | — |
| 20-23 | payload length | uint32 BE, bytes of encoded word list | — |
| 24-27 | checksum | uint32 BE, CRC32(payload) | — |
| 28-35 | stable id | uint64 BE, first 8 bytes SHA256(slug + source) | — |
| 36-37 | dominant count | uint16 BE | 6 |
| 38 | residual intensity | uint8, dominant % | 50 |
| 39 | flags / reserved | uint8, 0x00 for v2 | 0x00 |
| 40-47 | reserved | 8 bytes zero, future parent/parity pointer | 0x00 |

Total: 48 bytes = 16 pixels.

Category enum:
0 OTHER, 1 PERSON, 2 GPE, 3 LOC, 4 ORG, 5 PRODUCT, 6 NOUN,
7 PROPN, 8 VERB, 9 ADJ, 10 ADV

## Payload after header
Payload is the ordered word list, encoded after the header in the
visual area, and also checksummed by bytes 24-27:
- per word: category enum (1 byte) + RGB (3 bytes, from palette) +
  word text (UTF-8, length-prefixed in the .md / index, visual label
  in the PNG for human reading)
- payload length in bytes 20-23 is the byte length of that encoded
  word list, not the PNG file size.

## Recovery with this primitive
- Moved file: header is in pixels, not metadata chunks, so move /
  metadata strip does not remove it. Filename invariants also survive.
- Corrupted file: recompute CRC32(payload) and compare to bytes
  24-27. Mismatch = detectable corruption. Length mismatch vs
  bytes 20-23 = truncated payload.
- Deleted .md: PNG alone answers id, title hash, source hash,
  dominant category, counts, palette version, word count, and
  checksum. Full word text is still visual in the bars; full
  machine-readable word list requires the payload encoding in v2.1
  or regeneration from source hash lookup.
- Two blue memories: filename DOM + r + HASH8 + TICK distinguish
  them in an index scan without opening either payload.

## What this primitive does NOT do (next step, not v2)
- No XOR parity, no parent, no sibling regrowth.
- No recovery of a deleted PNG from siblings alone.
- Header is lost if the top row is cropped. That is a known limit.

## Decision 2026-11-17 — Next boundary: Option 1, full word list inside pixels
Chosen over Option 2 (id-based pairing, .md remains list carrier).
Rationale: recovery-first. A PNG memory should answer its own word
list after a move or after its .md is deleted. Id pairing alone still
leaves the PNG blind to full words except for visual labels.
Option 1 includes id pairing anyway, because id is already in the
48-byte header and in the filename, so we get both.

### v2 scope
- Header: 48 bytes in first 16 pixels, as above.
- Binary word block: immediately after header, still inside pixels,
  in row 0 continuing right, then row 1 if needed.
  Per word, in order:
  - 1 byte category enum (0-10, same as header)
  - 1 byte word length in bytes (UTF-8, max 255)
  - N bytes UTF-8 word text
  - RGB is NOT stored per word; it is derived from category + palette v1.
    That saves 3 bytes per word and keeps palette version as the single
    source of truth.
  - Payload length in header bytes 20-23 = total bytes of this word block.
  - Checksum in header bytes 24-27 = CRC32(word block).
- .md becomes optional commentary: human summary, counts, source text
  for convenience. It is no longer required to answer category or
  word list. It still carries the same stable id in its filename and
  header, so pairing by id works when both files exist.
- Visual color bars remain after the binary block, for human reading.
  Binary block pixels are visually distinct (dark strip) so a human
  does not confuse them with color bars.
- Size estimate: 12-word memory, avg 4 letters/word = 12*(1+1+4)=72 bytes
  = 24 pixels. Header 16 pixels + word block 24 pixels = 40 pixels
  of non-visual data, plus visual bars. Acceptable for v2.
- Long texts: word block grows linearly. Cap v2 at 256 words per PNG;
  longer memories split into child PNGs, which sets up the parent/parity
  primitive next.

## Parity primitive — confirmed scope 2026-11-17
Parent carries child-id manifest + XOR parity of child word blocks.
Confirmed, no different split rule.

### Split rule
- Memory >256 words splits in order, sequential, max 256 words per child.
- Child N is NOT standalone. It is a member of a parent that can regrow it.
- Child header bytes 40-47 (reserved in v2) = parent stable id, uint64 BE.
  0x0000000000000000 = no parent, standalone.
- Child filename also carries parent short hash as an extra token, so a
  destroyed parent can be found from children by filename scan alone.

### Parent PNG layout
- Same 48-byte header style, first 16 pixels.
  - Dominant category = dominant of combined children counts.
  - Word count = total words across all children.
  - Tick = parent creation tick.
  - Title hash / source hash = hash of combined source text.
  - Payload length = manifest bytes + parity bytes.
  - Checksum = CRC32(manifest + parity).
  - Stable id = parent id, uint64.
  - Reserved bytes 40-47 = 0, parent has no parent in v2.
- Payload after header, in order:
  1. Manifest:
     - 1 byte child count, uint8, 1-255
     - Per child, in order:
       - 8 bytes child stable id, uint64 BE
       - 2 bytes child word count, uint16 BE
       - 4 bytes child payload length, uint32 BE
       - 4 bytes child source hash, uint32 BE
  2. XOR parity:
     - Length = max child payload length, in bytes.
     - Parity[i] = XOR of all child word-block bytes at position i,
       with shorter children zero-padded to max length.
     - Stored as raw bytes in pixels after the manifest.
- Parent carries no word text of its own, except title. It is manifest + parity.

### Recovery
- Missing child k: regrow by XOR(parent parity, XOR of all surviving
  sibling word blocks, zero-padded to max length), then truncate to
  child k payload length from manifest. Verify CRC32 / source hash
  from manifest. If checksum matches, child is restored.
- Destroyed parent: rebuild from children using filename invariants
  + child headers. Each child header bytes 40-47 gives parent id,
  child filenames give parent short hash, dominant category, tick.
  Manifest is rebuilt by ordering children by their sequence in
  filenames / by their word order, and parity is recomputed by XOR
  of child word blocks. Parent header is rebuilt from combined counts.
- Single-child split: no parity gain, parent is manifest only.
  Parity is useful at 2+ children.
- Crop of header row still loses header/parity. That limit remains.

## Directory layout — decided 2026-11-17
One memory = one directory. Parent and children live in the SAME
directory, not in separate parent/child directories.

```
huemem/
  memories/
    <slug>-<parent-hash8>/          # standalone, <=256 words
      HUEMv1_<DOM>_<TICK>_r<XX>_<HASH8>_<slug>.png
      HUEMv1_<DOM>_<TICK>_r<XX>_<HASH8>_<slug>.md   # optional
    <slug>-<parent-hash8>/          # split, >256 words
      HUEMv1_<DOM>_<TICK>_r<XX>_<HASH8>_<slug>-parent.png
      HUEMv1_<DOM>_<TICK>_r<XX>_<HASH8>_<slug>-child01.png
      HUEMv1_<DOM>_<TICK>_r<XX>_<HASH8>_<slug>-child02.png
      HUEMv1_<DOM>_<TICK>_r<XX>_<HASH8>_<slug>-parent.md  # optional
      ...-child01.md, ...-child02.md  # optional
```

Rationale: moving the whole directory moves the whole memory.
Moving a single file still works because id is in header + filename,
not in path. A flat `layers/` folder would force a full scan to find
which children belong to which parent; a per-memory directory makes
the grouping visible in one listing, and a missing child is visible
as manifest count vs files present.

## Detection — out of scope for v2, confirmed
No daemon or watcher in v2. No automatic detection, no automatic
regrowth. Detection is on-demand / manual:
- When a memory directory is opened, or when a verify command is run,
  compare manifest child count vs files present, and recompute CRC32.
- Mismatch = missing or corrupted child, then regrow manually with
  explicit permission.
This matches the standing rule: no autonomous action without explicit
permission. A future v3 watcher would need explicit opt-in.

## .md in v2 — still written, optional
Yes, .md is still written for human reading, but it is NOT required
for recovery. Each .md carries the same stable id as its PNG, plus
parent id when it is a child, so pairing by id works when both files
exist. If the .md is deleted, the PNG still answers id, hashes,
dominant category, counts, and full word list from pixels.

## v2 scope closed — 2026-11-17
- Header + word list in pixels.
- Filename carries class, tick, short hash, parent short hash for children.
- Split memories recover via parent parity or child-to-parent rebuild.
- Directory-per-memory layout.
- On-demand verification, no watcher.
- .md optional, paired by id.
Accepted limits: crop of header row loses binary block; single-child
split has no parity gain; detection is manual.
Implementation is the remaining step, not yet done in v1 script.

## Implementation constraints — accepted 2026-11-17
From thread lessons and format research. Follow before / during v2 build:
1. Shared layout definition first. 48-byte header + word-block layout
   in one place (single Python constant block / header file). Writer
   and verify both import it. No duplicated offset math.
2. Atomic write for every PNG. Write to temp file in same directory,
   fsync, then rename. Prevents half-written PNG being read as valid.
3. Version the header. Version byte in first bytes of 48-byte header.
   Decoder rejects / routes on version before reading rest.
4. Round-trip test before any split or parity. Write one standalone
   memory (<=256 words), run verify, delete .md, run verify again,
   confirm PNG still answers. Only then write two-child split and
   confirm parity recovery.
5. Filename and directory invariants are generated, not hand-typed.
   Writer computes short hash and parent hash and places them in
   filename and directory name. Verify checks filename tokens match
   header values.
6. No watcher, no background process. Verify is single-process,
   on-demand. Reads directory, compares counts, recomputes CRC32
   and parity, prints agreed format. Does not write files or regrow.
7. Exit artifacts written from same spec. After round-trip passes,
   generate README, issues template, and one example memory directory
   from same constants used by writer. No separate prose that can drift.

## Exit strategy — accepted 2026-11-17
Open-source release on GitHub, MIT or Apache-2.0.
README states recovery contract and accepted limits (crop of header
row, single-child no parity, manual verify), measured improvement
over sidecar-based files, verify output format, one example memory,
issues template for bottleneck reports (recovery time, header size,
word-list encoding cost).
Commercial path, if any, is a hosted index/sync that reads the same
open PNG format; format itself stays open.
Self-improving loop, on-demand and inspectable:
- System writes bottleneck observations as PNG cells in same format.
- Scheduled verify compares latency / overhead against prior cells.
- Threshold crossed -> writes proposal PNG (longer header, different
  parity, different layout).
- Decision PNG accepts or rejects.
- No encoder rewrite until decision PNG exists.

## Research summary — accepted 2026-11-17
Standard file-based memory relies on location (path + filename) and
optional metadata sidecars. Content-addressable systems invert this:
identifier is derived from content, same bytes resolve to same address
regardless of path. Metadata sidecars (.md, EXIF, tEXt) can be lost,
moved, or stripped by gallery resaves. Embedding in pixel data survives
resave because payload is the image itself, not a removable chunk.
XOR parity is the standard primitive for single-fault recovery:
P = A ⊕ B ⊕ C allows any one missing member to be rebuilt.
Filename invariants encoding class + short hash let an index scan
distinguish records without opening payload.

Improvement v2 provides over out-of-the-box:
- Moved PNG answers category, word list, hashes from pixels alone.
  Standard loses answer if sidecar absent.
- Two memories with same dominant hue distinguishable by filename
  tokens without opening either. Standard slug-only requires opening both.
- Corruption detectable by CRC32 recompute. Standard PNG has no
  embedded length/checksum for payload.
- Split memories recover missing child / destroyed parent via XOR
  parity + filename invariants. Standard requires external index/sidecar.
- Directory-per-memory makes missing child visible in one listing.
  Flat folders require full scan.

## Verify command — confirmed output format 2026-11-17
Command: `python3 verify.py <memory-dir>`
On-demand only. No watcher. No autonomous regrowth.
Three checks, and only these three:
1. manifest child count vs files present
2. per-child CRC32 match / mismatch
3. parent parity CRC match, when 2+ children exist

Output format (human lines, then JSON):
```
HUEM verify: <slug>
dir: <path>
parent: <parent-id> <filename>
manifest: expected 3 children, found 2 — MISSING 1
child 1: <child-id> child01.png — CRC32 expected a1b2c3d4, actual a1b2c3d4 — MATCH
child 2: <child-id> child02.png — MISSING
child 3: <child-id> child03.png — CRC32 expected 9f8e7d6c, actual 9f8e7d00 — MISMATCH
parity: expected 11223344, actual 11223344 — MATCH
overall: FAIL — missing: child02, mismatched: child03
```
Standalone memory (no parent, 1 file):
```
HUEM verify: ada-sample
dir: memories/ada-sample-a1b2c3d4/
manifest: expected 1 child, found 1 — OK
child 1: <id> HUEMv1_NOUN_20261117_r50_a1b2c3d4_ada-sample.png — CRC32 expected ..., actual ... — MATCH
parity: N/A — single child, manifest only
overall: PASS
```
JSON after the human lines carries the same fields:
`{slug, dir, parent_id, expected, found, missing:[], children:[{id, filename, expected_crc, actual_crc, status}], parity:{expected_crc, actual_crc, status}, overall}`
A FAIL does not trigger regrowth. Regrowth is a separate manual
command with explicit permission.

Confirmed starting set: length + checksum + id + dominant-category
byte, plus magic, version, tick, title hash, source hash, word count,
dominant count, residual intensity, for a total of 48 bytes.
