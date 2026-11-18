#!/usr/bin/env python3
"""HueMem accelerator — fast reads, derived index, fault boundaries, scaling.

This module answers three skeptic questions about the PNG memory system
with code, not just prose (see ACCELERATOR.md for the full write-up with
measured numbers):

1. "Why PNG instead of SQLite / Parquet / Flatbuffers?"
   The PNG is the *authoritative* record; the accelerator's derived
   index (JSON + .npy matrices) is the *query-time* structure, and it
   is exactly the SQLite/Parquet-shaped thing — a regenerable index
   decoupled from the archive. The PNG earns its keep as the archive
   because it is inspectable with zero dependencies on any phone or
   edge device (any image viewer opens it), it is natively indexed by
   the OS media store (gallery, backup, sync — the gallery is the
   shared substrate), and corruption is *visible* (a damaged PNG shows
   up in the image; a truncated SQLite file fails opaquely). The cost
   is real — zlib overhead and a PNG parser for a 48-byte header —
   and this module pays it down: `read_header_fast` decompresses only
   the first row of the PNG instead of decoding the whole image, and
   `scan_headers` sweeps a directory of headers without touching a
   payload.

2. "What is the exact failure threshold of the parity math?"
   Single-erasure XOR parity *per split group* (RAID-5 style): a split
   memory survives the loss of any ONE member — the parent or exactly
   one child. Two erasures in the same group (two children, or parent
   + child) are unrecoverable from parity, because one parity equation
   cannot solve for two unknowns. Erasures in *different* groups are
   independent. A standalone memory has no parity (known limit); its
   .md sidecar still carries the source text, so a lost or corrupt
   standalone PNG can be rewritten from the sidecar. `fault_report`
   states the boundary per memory, read-only, and names the recovery
   action — it never performs it (regrow/rebuild need explicit
   confirm=True in huemem.py).

3. "What happens at N = 10,000?"
   Query time never touches a PNG. The derived index holds the token
   map and one embedding matrix per leg; a query is a posting-list
   lookup plus one BLAS matmul per leg — linear in N, vectorized.
   `accel_bench.py` measures the curve (latency, build cost, collision
   rate) so the scaling claim is a number, not an assertion.

Layout of the derived index (a directory):
  index.json   slugs (sorted), per-memory metadata, token postings,
               document frequencies, words, build stats
  emb_word.npy / emb_dist.npy / emb_plain.npy   float32, rows in slug order

The search mirrors huemem_recall.py exactly (same lex formula, same
synonym map, same RRF k=60, same gate 0.40/0.05) — accel_test.py checks
top-1 equivalence on all 43 golden prompts. No watcher, no autonomous
writes. Deleting the index loses nothing; rebuild it from the PNGs.

Usage:
  python3 huemem_accel.py headers <memories-dir>
  python3 huemem_accel.py fault <memory-dir>
  python3 huemem_accel.py build <memories-dir> <index-dir> [--dense-model ... | --lexical-only]
  python3 huemem_accel.py search <index-dir> "<query>" [--top N] [--abstain]
"""

import argparse, json, math, os, re, struct, sys, time, zlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from huemem import (HEADER_LEN, MAGIC, VERSION, unpack_header, read_png,
                    decode_payload, verify_dir, bare_slug, load_memory_words)
from huemem_recall import tokens, expand, parse_md

# ---------------------------------------------------------------------------
# 1. Streaming header-only PNG read
# ---------------------------------------------------------------------------

# PNG color types at bit depth 8: bytes per pixel.
_COLOR_BYTES = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _unfilter_row0(raw, bpp):
    """Undo the PNG row filter for row 0, where the prior row is all zeros.

    raw is filter_byte + row bytes. Filter types:
      0 None:   as-is
      1 Sub:    byte[i] += byte[i-bpp]
      2 Up:     byte[i] += 0 (prior row is zero)
      3 Avg:    byte[i] += floor(byte[i-bpp] / 2)
      4 Paeth:  byte[i] += Paeth(left, 0, 0) == left
    Only the first HEADER_LEN bytes of pixel data are needed, and every
    predictor above refers to bytes within those first bytes (or zero),
    so a partial row is enough.
    """
    ftype = raw[0]
    row = bytearray(raw[1:1 + HEADER_LEN])
    if ftype == 0:
        return bytes(row)
    if ftype == 1:
        for i in range(bpp, len(row)):
            row[i] = (row[i] + row[i - bpp]) & 0xFF
        return bytes(row)
    if ftype == 2:
        return bytes(row)
    if ftype == 3:
        for i in range(len(row)):
            left = row[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + (left >> 1)) & 0xFF
        return bytes(row)
    if ftype == 4:
        for i in range(len(row)):
            left = row[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + left) & 0xFF
        return bytes(row)
    raise ValueError(f"unknown PNG filter type {ftype}")


def read_header_fast(path):
    """Read a HUEM header by streaming only the first row of the PNG.

    Parses PNG chunks by hand, feeds IDAT data to a zlib decompressor
    with a byte cap, and stops as soon as the first HEADER_LEN bytes of
    row 0 are available. Returns (header_dict, stats) where stats
    records file size, compressed bytes consumed, and filter type.
    Precision notes: the file itself is read fully into memory
    (Path.read_bytes) — only the *decompression* is partial. Chunk
    CRCs are not checked, so this is a header scan, not an integrity
    check; use huemem.read_png / verify_dir for integrity.
    Raises ValueError on anything this fast path cannot handle
    (interlaced image, bit depth != 8, unknown color type) — the caller
    should fall back to the full read_png in that case.
    """
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos, width = 8, None
    idat = bytearray()
    decomp = zlib.decompressobj()
    got = b""
    stats = {"file_bytes": len(data), "compressed_bytes": 0, "filter": None}
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = \
                struct.unpack(">IIBBBBB", body)
            if interlace != 0:
                raise ValueError("interlaced PNG not supported by fast path")
            if bit_depth != 8:
                raise ValueError(f"bit depth {bit_depth} not supported by fast path")
            if color_type not in _COLOR_BYTES:
                raise ValueError(f"color type {color_type} not supported by fast path")
            bpp = _COLOR_BYTES[color_type]
            row_bytes = width * bpp
            if row_bytes < HEADER_LEN:
                raise ValueError("image row too short for a header")
        elif ctype == b"IDAT":
            idat.extend(body)
            stats["compressed_bytes"] += len(body)
            # Decompress just enough for filter byte + HEADER_LEN row bytes.
            got = decomp.decompress(bytes(idat), 1 + HEADER_LEN)
            idat = bytearray(decomp.unconsumed_tail)
            if len(got) >= 1 + HEADER_LEN:
                break
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if len(got) < 1 + HEADER_LEN:
        raise ValueError("could not decompress the first row")
    stats["filter"] = got[0]
    header_bytes = _unfilter_row0(got, bpp)
    header = unpack_header(header_bytes)
    return header, stats


def scan_headers(memories_dir):
    """Header-only sweep of every memory dir: identity + integrity, no payloads.

    Returns a list of {dir, slug, header, stats} plus totals. This is the
    cheap "is anything new / missing / renamed" pass: it never decodes a
    word block, so it stays fast as the corpus grows.
    """
    out, t0 = [], time.time()
    for d in sorted(os.listdir(memories_dir)):
        dpath = os.path.join(memories_dir, d)
        if not os.path.isdir(dpath):
            continue
        pngs = [f for f in os.listdir(dpath) if f.endswith(".png")]
        if not pngs:
            continue
        for f in sorted(pngs):
            try:
                h, st = read_header_fast(os.path.join(dpath, f))
                out.append({"dir": d, "file": f, "header": h, "stats": st,
                            "ok": True})
            except Exception as e:
                out.append({"dir": d, "file": f, "header": None, "stats": {},
                            "ok": False, "error": str(e)})
    return {"entries": out, "seconds": time.time() - t0,
            "count": len(out), "errors": sum(1 for e in out if not e["ok"])}


# ---------------------------------------------------------------------------
# 2. Derived index: build / load / vectorized search
# ---------------------------------------------------------------------------

def build_index(memories_dir, out_dir, dense_model="all-mpnet-base-v2",
                lexical_only=False):
    """One-time build of the derived index from the authoritative PNGs.

    Full PNG reads happen here and only here. Query time (AccelIndex)
    never opens a PNG. Returns the index dict and writes index.json +
    one .npy matrix per dense leg.
    """
    import numpy as np
    t0 = time.time()
    slugs, words, metas, sources, dists, plains = [], {}, {}, {}, {}, {}
    for d in sorted(os.listdir(memories_dir)):
        dpath = os.path.join(memories_dir, d)
        if not os.path.isdir(dpath):
            continue
        pngs = [f for f in os.listdir(dpath) if f.endswith(".png")]
        mds = [f for f in os.listdir(dpath) if f.endswith(".md")]
        if not pngs:
            continue
        slug = bare_slug(d)
        if slug in words:
            print(f"warning: duplicate memory dir for slug {slug} ({d}) — skipped",
                  file=sys.stderr)
            continue
        tagged, header, _is_split = load_memory_words(dpath)
        w = [x for x, _ in tagged]
        src, dist, plain = parse_md(os.path.join(dpath, mds[0])) if mds else ("", "", "")
        slugs.append(slug)
        words[slug] = w
        metas[slug] = {"dir": d, "dominant": header["dominant"], "tick": header["tick"],
                       "stable_id": header["stable_id"],
                       "word_count": header["word_count"],
                       "file_bytes": os.path.getsize(os.path.join(dpath, pngs[0])),
                       "crc_ok": header.get("crc_ok", False)}
        sources[slug] = src
        dists[slug] = dist or src
        plains[slug] = plain
    N = len(slugs)
    idx_of = {s: i for i, s in enumerate(slugs)}
    mem_tokens, df, postings = {}, {}, {}
    for s in slugs:
        mt = expand(set(tokens(" ".join(words[s]))) | set(tokens(s)))
        mem_tokens[s] = sorted(mt)
        for t in mt:
            df[t] = df.get(t, 0) + 1
            postings.setdefault(t, []).append(idx_of[s])
    emb = {}
    if not lexical_only and dense_model:
        from sentence_transformers import SentenceTransformer
        enc = SentenceTransformer(dense_model)
        has_plain = any(plains[s] for s in slugs)
        for name, texts in (("word", [" ".join(words[s]) for s in slugs]),
                            ("dist", [dists[s] for s in slugs]),
                            ("plain", [plains[s] or sources[s] for s in slugs])):
            if name == "plain" and not has_plain:
                continue
            e = enc.encode(texts, normalize_embeddings=True)
            emb[name] = np.asarray(e, dtype=np.float32)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, mat in emb.items():
        np.save(out / f"emb_{name}.npy", mat)
    index = {"version": 1, "built_at": time.time(), "model": dense_model,
             "lexical_only": bool(lexical_only),
             "slugs": slugs, "words": words, "meta": metas,
             "sources": sources, "dist": dists, "plain": plains,
             "postings": postings, "df": df,
             "has_plain": any(plains[s] for s in slugs),
             "build_seconds": time.time() - t0, "count": N}
    (out / "index.json").write_text(json.dumps(index))
    return index


class AccelIndex:
    """Query-time index: posting lists + embedding matrices, zero file I/O."""

    def __init__(self, index_dir):
        import numpy as np
        self.np = np
        self.dir = Path(index_dir)
        self.index = json.loads((self.dir / "index.json").read_text())
        self.slugs = self.index["slugs"]
        self.N = len(self.slugs)
        self.idx_of = {s: i for i, s in enumerate(self.slugs)}
        self.df = self.index["df"]
        self.postings = self.index["postings"]
        self.has_plain = self.index["has_plain"]
        self.emb = {}
        for name in ("word", "dist", "plain"):
            p = self.dir / f"emb_{name}.npy"
            if p.exists():
                self.emb[name] = np.load(p)
        self.encoder = None

    def _idf(self, t):
        return math.log(self.N / max(1, self.df.get(t, 1))) + 1.0

    def lex_scores(self, prompt):
        """Same formula as huemem_recall.RecallIndex.lex_scores, via postings."""
        qt = expand(set(tokens(prompt)))
        out = {s: 0.0 for s in self.slugs}
        slug_toks = {s: set(tokens(s)) for s in self.slugs}
        for t in sorted(qt):
            w = self._idf(t) * (2.0 if self.df.get(t, 99) <= 2 else 1.0)
            for i in self.postings.get(t, []):
                out[self.slugs[i]] += w
            for s in self.slugs:
                if t in slug_toks[s]:
                    out[s] += 0.5
        return out

    def _rank(self, scores):
        """Rank dict slug -> position, score desc, slug asc on ties."""
        order = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return {s: i for i, (s, _) in enumerate(order)}

    def _dense(self, prompt):
        """One matmul per leg over the stacked matrices (vectorized)."""
        if self.encoder is None:
            from sentence_transformers import SentenceTransformer
            self.encoder = SentenceTransformer(self.index["model"])
        qe = self.encoder.encode([prompt], normalize_embeddings=True)[0]
        out = {}
        for name, mat in self.emb.items():
            out[name] = {s: float(v) for s, v in
                         zip(self.slugs, mat @ qe)}
        return out

    def search(self, prompt, top=5, abstain=False, gate=True,
               gate_tau_s=0.40, gate_tau_m=0.05,
               abstain_plain_tau=0.25, abstain_tau=0.20):
        """Same contract as huemem_recall.RecallIndex.search.

        Gate on + plain sentences present: plain-cos top-1 >= tau_s and
        margin >= tau_m -> plain ranking, else three-way RRF (k=60).
        Abstention uses the plain-cos threshold (0.25) or, without plain
        sentences, the word-cos threshold (0.20) — the same operating
        points as huemem_recall. (The Round 24 logistic lives in
        huemem_recall; this index exposes the same score inputs.)
        """
        lex = self.lex_scores(prompt)
        ranks = [self._rank(lex)]
        dense = self._dense(prompt) if self.emb else {}
        word_cos = dense.get("word")
        plain_cos = dense.get("plain")
        if word_cos:
            ranks.append(self._rank(word_cos))
        if "dist" in dense:
            ranks.append(self._rank(dense["dist"]))
        plain_ranking = None
        if gate and plain_cos is not None:
            sc = sorted(plain_cos.values(), reverse=True)
            margin = sc[0] - sc[1] if len(sc) > 1 else float("inf")
            if sc[0] >= gate_tau_s and margin >= gate_tau_m:
                plain_ranking = sorted(plain_cos.items(),
                                       key=lambda kv: (-kv[1], kv[0]))
        if plain_ranking is not None:
            ranked = plain_ranking[:top]
            if abstain and plain_cos is not None and \
                    max(plain_cos.values()) < abstain_plain_tau:
                return []
            return [(s, score, (word_cos or {}).get(s))
                    for s, score in ranked]
        fused = {s: sum(1 / (60 + r[s] + 1) for r in ranks)
                 for s in self.slugs}
        ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
        if abstain:
            if plain_cos is not None and max(plain_cos.values()) < abstain_plain_tau:
                return []
            if plain_cos is None and word_cos is not None and \
                    word_cos[ranked[0][0]] < abstain_tau:
                return []
        return [(s, score, (word_cos or {}).get(s))
                for s, score in ranked[:top]]


# ---------------------------------------------------------------------------
# 3. Parity fault boundary (read-only report)
# ---------------------------------------------------------------------------

def fault_report(memory_dir):
    """State the exact fault-tolerance boundary for one memory directory.

    Single-erasure XOR parity per split group (RAID-5 style):
      - standalone: no parity. PNG lost/corrupt + .md sidecar present
        -> rewrite from sidecar (source text is in the sidecar, and the
        stable id is derived from slug+source, so the rewrite is
        identical bar the tick). PNG + sidecar both gone -> lost.
      - split, 0 erasures -> intact.
      - split, exactly 1 erasure -> recoverable. Parent missing ->
        rebuild_parent; one child missing/corrupt -> regrow_missing.
      - split, >= 2 erasures (two children, or parent + child) ->
        UNRECOVERABLE from parity: one parity equation cannot solve
        for two unknowns. A corrupt parent counts as a parent erasure
        (its manifest/parity are untrusted), but intact children can
        still be re-parented — the missing child's words are lost.
        When the parent is unreadable or its payload CRC fails, the
        child list is enumerated from the files on disk instead of the
        untrusted manifest, and the report says plainly that the
        expected child count is unknown without it. A deleted parent
        is the same as a corrupt one: the children on disk are the
        record, and they rebuild the parent.
    Erasures in different split groups are independent: one lost child
    in group A and one in group B is fine.
    This function only reports; regrow/rebuild need explicit
    confirm=True in huemem.py. It never raises on the damage it exists
    to describe: an unreadable parent or undecodable manifest is
    reported as a parent erasure.
    """
    dpath = Path(memory_dir)
    pngs = sorted(dpath.glob("*.png"))
    mds = sorted(dpath.glob("*.md"))
    sidecar_src = None
    if mds:
        for line in open(mds[0], encoding="utf-8", errors="replace"):
            if line.startswith("- source: "):
                sidecar_src = line[len("- source: "):].strip()
    # Parent/child files are matched by the writer's actual suffixes,
    # never by substring — a slug containing "-parent" or "-child" must
    # not match.
    parents = [p for p in pngs if p.name.endswith("-parent.png")]
    child_files = [p for p in pngs if re.search(r"child\d+\.png$", p.name)]
    if not parents and not child_files:
        # Standalone (or empty dir).
        if not pngs:
            return {"kind": "standalone", "dir": str(dpath), "erasures": ["png"],
                    "recoverable": bool(sidecar_src),
                    "action": "rewrite_from_sidecar" if sidecar_src else None,
                    "detail": "PNG gone; sidecar holds the source text, so the "
                              "memory can be rewritten identically (stable id "
                              "derives from slug+source)." if sidecar_src else
                              "PNG and sidecar both gone: memory lost."}
        try:
            h, _, _ = read_png(pngs[0])
            ok = bool(h.get("crc_ok"))
        except Exception as e:
            return {"kind": "standalone", "dir": str(dpath),
                    "erasures": [f"png unreadable ({e})"],
                    "recoverable": bool(sidecar_src),
                    "action": "rewrite_from_sidecar" if sidecar_src else None,
                    "detail": "PNG unreadable; rewrite from sidecar." if sidecar_src
                              else "PNG unreadable and no sidecar: memory lost."}
        if ok:
            return {"kind": "standalone", "dir": str(dpath), "erasures": [],
                    "recoverable": True, "action": "none", "detail": "intact."}
        return {"kind": "standalone", "dir": str(dpath),
                "erasures": ["png CRC mismatch"],
                "recoverable": bool(sidecar_src),
                "action": "rewrite_from_sidecar" if sidecar_src else None,
                "detail": "PNG payload corrupt (CRC mismatch); rewrite from "
                          "sidecar." if sidecar_src else
                          "PNG corrupt and no sidecar: memory lost."}
    # Split group: enumerate the children on disk (CRC-checked).
    disk_good, disk_bad = [], []
    for p in child_files:
        try:
            ch, _, _ = read_png(p)
            (disk_good if ch.get("crc_ok") else disk_bad).append(p.name)
        except Exception:
            disk_bad.append(p.name)
    # Child numbers from the filenames: a gap in 1..N means a child is
    # missing even without the manifest (the manifest is gone with the
    # parent or untrusted). A missing HIGHEST child is undetectable
    # this way — the surviving numbers still run 1..N — and the report
    # says so wherever it rebuilds without a manifest.
    nums = sorted(int(m.group(1)) for p in child_files
                  for m in [re.search(r"child(\d+)\.png$", p.name)] if m)
    seq_gap = (not nums) or (nums != list(range(1, len(nums) + 1)))
    if not parents:
        # Parent file gone: the children on disk are the only record.
        erasures = ["parent (missing)"] + \
            [f"child file {n} (unreadable/corrupt)" for n in disk_bad]
        if disk_bad or seq_gap:
            if seq_gap and not disk_bad:
                erasures.append("child (missing — gap in the child-number sequence)")
            return {"kind": "split", "dir": str(dpath), "erasures": erasures,
                    "recoverable": False, "action": None,
                    "detail": f"{len(erasures)} erasures in one split group: "
                              f"the parent (and its parity) is gone and a "
                              f"child is missing or unreadable — one parity "
                              f"equation cannot solve for two unknowns. The "
                              f"lost child's words are unrecoverable. "
                              f"(Surviving children can still be re-parented, "
                              f"but the missing words stay lost.)"}
        return {"kind": "split", "dir": str(dpath), "erasures": erasures,
                "recoverable": True, "action": "rebuild_parent",
                "detail": f"Single erasure (parent). All {len(disk_good)} "
                          f"children present and CRC-ok: rebuild the parent "
                          f"— manifest + parity — from the children. Caveat: "
                          f"without the manifest a missing highest-numbered "
                          f"child is undetectable, so the rebuild preserves "
                          f"the children found but cannot confirm none are "
                          f"missing."}
    # Parent present: is its manifest trustworthy?
    parent_ok, parent_error, res, verify_ok = True, None, None, True
    try:
        ph, _, _ = read_png(parents[0])
        parent_ok = bool(ph.get("crc_ok"))
        if not parent_ok:
            parent_error = "payload CRC mismatch"
    except Exception as e:
        parent_ok = False
        parent_error = f"unreadable ({e})"
    if parent_ok:
        try:
            res = verify_dir(dpath)
        except Exception as e:
            verify_ok = False
            verify_error = str(e)
    if not parent_ok or not verify_ok:
        # Manifest/parity untrusted: enumerate from disk, not the manifest.
        erasures = [f"parent ({parent_error or 'manifest undecodable: ' + verify_error})"] + \
            [f"child file {n} (unreadable/corrupt)" for n in disk_bad]
        if seq_gap:
            erasures.append("child (missing — gap in the child-number sequence)")
            return {"kind": "split", "dir": str(dpath), "erasures": erasures,
                    "recoverable": False, "action": None,
                    "detail": f"{len(erasures)} erasures in one split group: "
                              f"the parent manifest/parity is untrusted and a "
                              f"child is missing — one parity equation cannot "
                              f"solve for two unknowns. The lost child's words "
                              f"are unrecoverable."}
        return {"kind": "split", "dir": str(dpath), "erasures": erasures,
                "recoverable": True, "action": "rebuild_parent",
                "detail": f"Parent manifest/parity untrusted "
                          f"({parent_error or verify_error}). "
                          f"{len(disk_good)} intact children found on disk; "
                          f"rebuild the parent — manifest + parity — from "
                          f"them. Caveat: without the manifest the expected "
                          f"child count is unknown, so a missing "
                          f"highest-numbered child would go unnoticed by the "
                          f"rebuild."}
    # Parent fine: trust the manifest.
    erasures = []
    missing_index = None
    for i, c in enumerate(res["children"], start=1):
        if c["status"] == "MISSING":
            erasures.append(f"child {c['id']} (missing)")
            missing_index = i
        elif c["status"] != "MATCH":
            erasures.append(f"child {c['id']} ({c['status']})")
            missing_index = i
    n = len(erasures)
    if n == 0:
        return {"kind": "split", "dir": str(dpath), "erasures": [],
                "recoverable": True, "action": "none",
                "detail": f"intact: {res['found']}/{res['expected']} children, "
                          f"parity {res['parity']['status']}."}
    if n == 1:
        return {"kind": "split", "dir": str(dpath), "erasures": erasures,
                "recoverable": True, "action": "regrow_missing",
                "missing_index": missing_index,
                "detail": "Single erasure (one child). Regrow it from the "
                          "parent parity XOR the surviving siblings "
                          f"(manifest child {missing_index})."}
    return {"kind": "split", "dir": str(dpath), "erasures": erasures,
            "recoverable": False, "action": None,
            "detail": f"{n} erasures in one split group: one XOR parity "
                      f"equation cannot solve for two unknowns. The lost "
                      f"children's words are unrecoverable from parity. "
                      f"(Surviving children can still be re-parented, but "
                      f"the missing words stay lost.)"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="HueMem accelerator.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sh = sub.add_parser("headers"); sh.add_argument("mem_dir")
    sf = sub.add_parser("fault"); sf.add_argument("memory_dir")
    sb = sub.add_parser("build"); sb.add_argument("mem_dir"); sb.add_argument("out_dir")
    sb.add_argument("--dense-model", default="all-mpnet-base-v2")
    sb.add_argument("--lexical-only", action="store_true")
    ss = sub.add_parser("search"); ss.add_argument("index_dir"); ss.add_argument("query")
    ss.add_argument("--top", type=int, default=5)
    ss.add_argument("--abstain", action="store_true")
    ss.add_argument("--no-gate", action="store_true")
    args = ap.parse_args()
    if args.cmd == "headers":
        r = scan_headers(args.mem_dir)
        print(f"headers: {r['count']} PNGs in {r['seconds']:.3f}s "
              f"({1000 * r['seconds'] / max(r['count'], 1):.2f} ms/PNG), "
              f"errors: {r['errors']}")
        return 0
    if args.cmd == "fault":
        print(json.dumps(fault_report(args.memory_dir), indent=2, default=str))
        return 0
    if args.cmd == "build":
        idx = build_index(args.mem_dir, args.out_dir,
                          dense_model=None if args.lexical_only else args.dense_model,
                          lexical_only=args.lexical_only)
        print(f"built index: {idx['count']} memories in {idx['build_seconds']:.1f}s "
              f"-> {args.out_dir}")
        return 0
    idx = AccelIndex(args.index_dir)
    hits = idx.search(args.query, top=args.top, abstain=args.abstain,
                      gate=not args.no_gate)
    if not hits:
        print("abstained: no memory clears the threshold")
        return 0
    for slug, score, cos in hits:
        print(f"{slug}  score={score:.4f}" +
              (f"  word_cos={cos:.2f}" if cos is not None else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
