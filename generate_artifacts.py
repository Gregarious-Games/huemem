#!/usr/bin/env python3
"""Generate exit artifacts from huemem constants. No hand-typed drift."""
from pathlib import Path
from huemem import MAGIC, VERSION, HEADER_LEN, HEADER_PIXELS, MAX_WORDS, PALETTE, CAT_TO_CODE

ROOT = Path(__file__).parent
OUT = ROOT / "exit"
(OUT / ".github" / "ISSUE_TEMPLATE").mkdir(parents=True, exist_ok=True)

palette_lines = "\n".join(f"- {k} RGB{v} — code {CAT_TO_CODE[k]}" for k, v in PALETTE.items())

readme = f"""# HueMem v{VERSION}

Self-describing PNG memory. License: MIT or Apache-2.0.

## Recovery contract
- Header: {HEADER_LEN} bytes in first {HEADER_PIXELS} pixels, magic {MAGIC!r}, version {VERSION}.
- Word list in pixels. A moved PNG answers category, word list, and hashes without its .md.
- Filename: `HUEMv1_<DOM>_<TICK>_r<XX>_<HASH8>_<slug>.png`
- Directory-per-memory: `memories/<slug>-<hash8>/`
- Split memories: max {MAX_WORDS} words per child, parent carries manifest + XOR parity.
- Verify is on-demand only. No watcher. No autonomous regrowth.
- .md is optional commentary, paired by stable id.

## Accepted limits
- Crop of the header row loses the binary block.
- Single-child split has no parity gain.
- Detection is manual, via verify.

## Palette v{VERSION}
{palette_lines}

## Verify
```
python3 huemem.py verify <memory-dir>
```
Reports:
- manifest child count vs files present
- per-child CRC32 match / mismatch
- parent parity CRC match when 2+ children exist

Overall: PASS or FAIL, with missing and mismatched lists.
FAIL does not trigger regrowth. Regrowth is a separate manual command with explicit permission.

## Example memory
No example memory is included in the public release — it contains only the capability, no memories.
Create your own with `write_standalone()` / `write_split()` in `huemem.py`, then run `python3 huemem.py verify <memory-dir>` to see PASS.

## Build
Shared layout lives in `huemem.py`. Writer, reader, verify, regrow, and rebuild-parent all import it. No duplicated offset math. PNGs are written atomically: temp file, fsync, rename.
"""

(OUT / "README.md").write_text(readme)

issue = """---
name: Bottleneck report
about: Report a recovery bottleneck in HueMem v1
---

## Bottleneck
<!-- recovery time, header size, word-list encoding cost, verify time -->

## Memory
<!-- slug, word count, child count, dominant category -->

## Measured
<!-- verify output, file sizes, CRC mismatch if any -->

## Expected
<!-- what should have happened under the v2 spec -->
"""

(OUT / ".github" / "ISSUE_TEMPLATE" / "bottleneck.md").write_text(issue)
print(f"Wrote {OUT/'README.md'}")
print(f"Wrote {OUT/'.github/ISSUE_TEMPLATE/bottleneck.md'}")
print(f"Palette entries: {len(PALETTE)}, header {HEADER_LEN} bytes, max words {MAX_WORDS}")
