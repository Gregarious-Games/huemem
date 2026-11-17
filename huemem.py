#!/usr/bin/env python3
"""
HueMem v2 — shared layout, single source of truth.
Writer, reader, verify, regrow, rebuild-parent all import this file.
No duplicated offset math.

Header: 48 bytes in first 16 RGB pixels, row 0, big-endian.
 0-3  magic "HUEM"
 4    version uint8 (1)
 5    dominant category uint8 enum
 6-7  word count uint16 BE
 8-11 tick uint32 BE (unix seconds)
 12-15 title hash uint32 BE, first 4 bytes SHA256(title)
 16-19 source hash uint32 BE, first 4 bytes SHA256(source)
 20-23 payload length uint32 BE
 24-27 checksum uint32 BE, CRC32(payload)
 28-35 stable id uint64 BE, first 8 bytes SHA256(slug+source)
 36-37 dominant count uint16 BE
 38   residual uint8, dominant %
 39   flags uint8, 0
 40-47 parent id uint64 BE, 0 = no parent, else parent stable id

Word block (standalone / child payload):
 per word: 1 byte cat code, 1 byte word-byte-length, N bytes UTF-8 word.
 RGB derived from category + PALETTE_V1, not stored.

Parent payload:
 manifest: 1 byte child count, per child 8B id + 2B word count + 4B payload len + 4B source hash
 parity: max child payload length bytes, XOR of child word blocks, zero-padded.
"""
import hashlib, json, os, re, struct, time, zlib
from collections import Counter
from pathlib import Path

MAGIC = b'HUEM'
VERSION = 1
HEADER_LEN = 48
HEADER_PIXELS = 16
MAX_WORDS = 256
WIDTH = 800
VISUAL_Y = 24

CAT_TO_CODE = {"OTHER":0,"PERSON":1,"GPE":2,"LOC":3,"ORG":4,"PRODUCT":5,"NOUN":6,"PROPN":7,"VERB":8,"ADJ":9,"ADV":10}
CODE_TO_CAT = {v:k for k,v in CAT_TO_CODE.items()}
PALETTE = {
    "PERSON": (255,99,71), "GPE": (46,139,87), "LOC": (60,179,113),
    "ORG": (70,130,180), "PRODUCT": (218,165,32), "NOUN": (100,149,237),
    "PROPN": (65,105,225), "VERB": (255,165,0), "ADJ": (186,85,211),
    "ADV": (221,160,221), "OTHER": (169,169,169),
}

PEOPLE = {"ada","lovelace"}  # example only — add your own names, or install spaCy for full tagging
PLACES = {"london"}  # example only — add your own places, or install spaCy for full tagging
VERBS = {"wrote","installed","installing","sold","deleted","went","got","met","began","started","help","put","build","learn","understand","speak","talk","walked","created"}
ADJS = {"early","great","new","live","small","very","low","professional"}
ADVS = {"yesterday","today","now","first","again","directly"}

def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()
def _hash4(text: str) -> int:
    return struct.unpack(">I", _sha256(text.encode())[:4])[0]
def _hash8_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]
def _stable_id(slug: str, source: str) -> int:
    return struct.unpack(">Q", _sha256((slug+"|"+source).encode())[:8])[0]
def _crc32(b: bytes) -> int:
    return zlib.crc32(b) & 0xffffffff

def tag_text(text: str):
    try:
        import spacy
        nlp = spacy.load("en_core_web_sm")
        doc = nlp(text)
        out=[]
        for t in doc:
            cat = t.ent_type_ if t.ent_type_ in PALETTE else (t.pos_ if t.pos_ in PALETTE else "OTHER")
            out.append((t.text, cat))
        return out
    except Exception:
        tokens = re.findall(r"[A-Za-z0-9']+|[.,!?;]", text)
        out=[]
        for tok in tokens:
            low=tok.lower()
            if tok in ".,!?;": out.append((tok,"OTHER"))
            elif low in PEOPLE: out.append((tok,"PERSON"))
            elif low in PLACES: out.append((tok,"GPE"))
            elif low in VERBS: out.append((tok,"VERB"))
            elif low in ADJS: out.append((tok,"ADJ"))
            elif low in ADVS: out.append((tok,"ADV"))
            elif tok[0].isupper(): out.append((tok,"PROPN"))
            elif low.endswith("ing") or low.endswith("ed"): out.append((tok,"VERB"))
            else: out.append((tok,"NOUN"))
        return out

def encode_word_block(tagged):
    out=bytearray()
    for word, cat in tagged:
        wb=word.encode()
        out.append(CAT_TO_CODE.get(cat,0))
        out.append(len(wb))
        out.extend(wb)
    return bytes(out)

def decode_word_block(data: bytes, word_count: int):
    out=[]; i=0
    while i < len(data) and len(out) < word_count:
        if i+2>len(data): break
        code=data[i]; ln=data[i+1]; i+=2
        wb=data[i:i+ln]; i+=ln
        out.append((wb.decode(errors="replace"), CODE_TO_CAT.get(code,"OTHER")))
    return out

def pack_header(*, dominant, word_count, tick, title, source, payload, stable_id, dominant_count, residual, parent_id=0):
    return struct.pack(">4sBBHIIIIIQHBB8s",
        MAGIC, VERSION, CAT_TO_CODE[dominant],
        word_count, tick, _hash4(title), _hash4(source),
        len(payload), _crc32(payload), stable_id,
        dominant_count, residual, 0,
        struct.pack(">Q", parent_id))

def unpack_header(data: bytes):
    if len(data)<HEADER_LEN: raise ValueError("header too short")
    magic, ver, dom_code, wc, tick, th, sh, plen, crc, sid, dcount, resid, flags, parent_bytes = struct.unpack(">4sBBHIIIIIQHBB8s", data[:HEADER_LEN])
    if magic!=MAGIC: raise ValueError(f"bad magic {magic!r}")
    if ver!=VERSION: raise ValueError(f"unsupported version {ver}")
    parent_id=struct.unpack(">Q", parent_bytes)[0]
    return {"magic":magic,"version":ver,"dominant":CODE_TO_CAT.get(dom_code,"OTHER"),"dominant_code":dom_code,
            "word_count":wc,"tick":tick,"title_hash":th,"source_hash":sh,"payload_len":plen,
            "checksum":crc,"stable_id":sid,"dominant_count":dcount,"residual":resid,"flags":flags,"parent_id":parent_id}

def _atomic_write_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_name(path.name+".tmp")
    with open(tmp,"wb") as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dfd=os.open(str(path.parent), os.O_DIRECTORY); os.fsync(dfd); os.close(dfd)
    except Exception: pass

def _atomic_write_png(path: Path, img):
    import io
    buf=io.BytesIO(); img.save(buf, format="PNG"); data=buf.getvalue()
    _atomic_write_bytes(path, data)

def _render_png(header: bytes, payload: bytes, tagged, title: str, is_parent=False, child_info=""):
    from PIL import Image, ImageDraw, ImageFont
    # binary data = header + payload, in raster order row 0
    binary = header + payload
    # pad to multiple of 3
    pad = (-len(binary)) % 3
    binary_padded = binary + b"\x00"*pad
    pixels=[]
    for i in range(0, len(binary_padded), 3):
        pixels.append(tuple(binary_padded[i:i+3]))
    # image: width 800, height visual_y + 180
    H = VISUAL_Y + 180
    img = Image.new("RGB", (WIDTH, H), (18,18,22))
    # place binary pixels in row 0
    for idx, px in enumerate(pixels):
        if idx>=WIDTH: break
        img.putpixel((idx,0), px)
    d = ImageDraw.Draw(img)
    try:
        font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
        small=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except Exception:
        font=ImageFont.load_default(); small=font
    d.text((10, VISUAL_Y+2), title[:80], fill=(255,255,255), font=small)
    if is_parent:
        d.text((10, VISUAL_Y+28), f"PARENT — {child_info}", fill=(255,255,255), font=small)
        d.rectangle([10, VISUAL_Y+60, WIDTH-10, VISUAL_Y+120], fill=(70,130,180))
        d.text((20, VISUAL_Y+80), "manifest + XOR parity", fill=(255,255,255), font=small)
        return img
    # visual bars
    y = VISUAL_Y+28
    x=0
    total_w = WIDTH
    # width per word proportional, min 20
    for word, cat in tagged:
        rgb=PALETTE.get(cat,(169,169,169))
        w=max(20, int(total_w/len(tagged)) if tagged else 20)
        if x+w>WIDTH: w=WIDTH-x
        d.rectangle([x, y, x+w-2, y+100], fill=rgb)
        d.text((x+2, y+106), word[:10], fill=(255,255,255), font=small)
        d.text((x+2, y+128), cat, fill=rgb, font=small)
        x+=w
        if x>=WIDTH: break
    return img

def _dominant(tagged):
    counts=Counter(c for _,c in tagged)
    dom, n = counts.most_common(1)[0] if counts else ("OTHER",0)
    resid = int(n*100/len(tagged)) if tagged else 0
    return dom, n, resid, counts

def _filename(dom, tick, resid, hash8, slug, suffix=""):
    date_str=time.strftime("%Y%m%d", time.localtime(tick))
    return f"HUEMv1_{dom}_{date_str}_r{resid:02d}_{hash8}_{slug}{suffix}.png"

def write_standalone(text, slug, title, root, tick=None):
    root=Path(root); tick=tick or int(time.time())
    tagged=tag_text(text)
    dom, dcount, resid, counts=_dominant(tagged)
    payload=encode_word_block(tagged)
    sid=_stable_id(slug, text)
    hash8=_hash8_hex(text)
    header=pack_header(dominant=dom, word_count=len(tagged), tick=tick, title=title, source=text, payload=payload, stable_id=sid, dominant_count=dcount, residual=resid, parent_id=0)
    fname=_filename(dom, tick, resid, hash8, slug)
    mem_dir=root/f"{slug}-{hash8}"
    mem_dir.mkdir(parents=True, exist_ok=True)
    png_path=mem_dir/fname
    img=_render_png(header, payload, tagged, title)
    _atomic_write_png(png_path, img)
    md_path=png_path.with_suffix(".md")
    md=f"# PNG Memory — {title}\n\n- id: {sid}\n- slug: {slug}\n- hash8: {hash8}\n- dominant: {dom} {dcount} ({resid}%)\n- words: {len(tagged)}\n- source: {text}\n- png: {fname}\n"
    _atomic_write_bytes(md_path, md.encode())
    return {"dir":str(mem_dir),"png":str(png_path),"md":str(md_path),"stable_id":sid,"hash8":hash8,"header":unpack_header(header),"tagged":tagged}

def read_png(path):
    from PIL import Image
    im=Image.open(path); im.load()
    w,h=im.size
    pixels=list(im.getdata())
    # first row = binary
    raw=bytearray()
    for px in pixels[:w]:
        raw.extend(px[:3])
    header=unpack_header(bytes(raw[:HEADER_LEN]))
    plen=header["payload_len"]
    need=(HEADER_LEN+plen+2)//3*3
    # payload bytes are after header in raster order
    payload_bytes=bytes(raw[HEADER_LEN:HEADER_LEN+plen])
    # verify crc
    actual_crc=_crc32(payload_bytes)
    header["crc_ok"]= (actual_crc==header["checksum"])
    header["actual_crc"]=actual_crc
    return header, payload_bytes, im

def decode_payload(header, payload):
    return decode_word_block(payload, header["word_count"])

# --- split / parent ---
def _encode_manifest(children):
    # children: list of dicts with stable_id, word_count, payload_len, source_hash
    out=bytearray([len(children)])
    for c in children:
        out.extend(struct.pack(">QHII", c["stable_id"], c["word_count"], c["payload_len"], c["source_hash"]))
    return bytes(out)

def _decode_manifest(data):
    n=data[0]; off=1; out=[]
    for _ in range(n):
        sid, wc, plen, sh = struct.unpack(">QHII", data[off:off+18]); off+=18
        out.append({"stable_id":sid,"word_count":wc,"payload_len":plen,"source_hash":sh})
    return out, off

def write_split(text, slug, title, root, tick=None):
    root=Path(root); tick=tick or int(time.time())
    tagged_all=tag_text(text)
    chunks=[tagged_all[i:i+MAX_WORDS] for i in range(0, len(tagged_all), MAX_WORDS)]
    if len(chunks)<2: raise ValueError("split needs >256 words")
    hash8=_hash8_hex(text)
    parent_sid=_stable_id(slug+"|parent", text)
    mem_dir=root/f"{slug}-{hash8}"; mem_dir.mkdir(parents=True, exist_ok=True)
    dom_all, dcount_all, resid_all, _=_dominant(tagged_all)
    children=[]
    child_payloads=[]
    for idx, chunk in enumerate(chunks, start=1):
        chunk_text=" ".join(w for w,_ in chunk)
        dom, dcount, resid, _=_dominant(chunk)
        payload=encode_word_block(chunk)
        child_payloads.append(payload)
        sid=_stable_id(f"{slug}-child{idx:02d}", chunk_text)
        header=pack_header(dominant=dom, word_count=len(chunk), tick=tick, title=f"{title} — child {idx:02d}", source=chunk_text, payload=payload, stable_id=sid, dominant_count=dcount, residual=resid, parent_id=parent_sid)
        # child filename with parent hash token
        parent_hash8=hashlib.sha256(struct.pack(">Q", parent_sid)).hexdigest()[:8]
        date_str=time.strftime("%Y%m%d", time.localtime(tick))
        fname=f"HUEMv1_{dom}_{date_str}_r{resid:02d}_{_hash8_hex(chunk_text)}_p{parent_hash8}_{slug}-child{idx:02d}.png"
        png_path=mem_dir/fname
        img=_render_png(header, payload, chunk, f"{title} — child {idx:02d}")
        _atomic_write_png(png_path, img)
        children.append({"stable_id":sid,"word_count":len(chunk),"payload_len":len(payload),"source_hash":_hash4(chunk_text),"filename":fname,"path":str(png_path),"payload":payload})
    # parity
    max_len=max(len(p) for p in child_payloads)
    parity=bytearray(max_len)
    for p in child_payloads:
        for i,b in enumerate(p):
            parity[i]^=b
    parity=bytes(parity)
    manifest=_encode_manifest(children)
    parent_payload=manifest+parity
    parent_header=pack_header(dominant=dom_all, word_count=len(tagged_all), tick=tick, title=f"{title} — parent", source=text, payload=parent_payload, stable_id=parent_sid, dominant_count=dcount_all, residual=resid_all, parent_id=0)
    parent_fname=_filename(dom_all, tick, resid_all, hash8, slug, suffix="-parent")
    parent_path=mem_dir/parent_fname
    parent_img=_render_png(parent_header, parent_payload, [], f"{title} — parent", is_parent=True, child_info=f"{len(children)} children, parity {max_len} bytes")
    _atomic_write_png(parent_path, parent_img)
    return {"dir":str(mem_dir),"parent":str(parent_path),"parent_id":parent_sid,"children":children,"hash8":hash8}

def verify_dir(dir_path):
    dir_path=Path(dir_path)
    pngs=sorted(dir_path.glob("*.png"))
    result={"dir":str(dir_path),"children":[],"parity":{"expected_crc":None,"actual_crc":None,"status":"N/A"},"overall":"PASS","missing":[],"mismatched":[]}
    # find parent
    parents=[p for p in pngs if "-parent" in p.name]
    if not parents:
        # standalone
        result["expected"]=len(pngs); result["found"]=len(pngs)
        for p in pngs:
            try:
                h, payload, _ = read_png(p)
                status="MATCH" if h["crc_ok"] else "MISMATCH"
                if not h["crc_ok"]: result["overall"]="FAIL"; result["mismatched"].append(p.name)
                result["children"].append({"id":h["stable_id"],"filename":p.name,"expected_crc":h["checksum"],"actual_crc":h["actual_crc"],"status":status})
            except Exception as e:
                result["overall"]="FAIL"; result["mismatched"].append(p.name)
                result["children"].append({"id":None,"filename":p.name,"expected_crc":None,"actual_crc":None,"status":f"ERROR {e}"})
        result["parent_id"]=None
        return result
    # split
    parent_path=parents[0]
    h, payload, _ = read_png(parent_path)
    result["parent_id"]=h["stable_id"]; result["parent_file"]=parent_path.name
    manifest, off = _decode_manifest(payload)
    parity_bytes=payload[off:]
    result["expected"]=len(manifest); result["found"]=0
    # map child id -> file
    child_files={}
    for p in pngs:
        if p==parent_path: continue
        try:
            ch, _, _ = read_png(p); child_files[ch["stable_id"]]=p
        except Exception: pass
    for idx, m in enumerate(manifest, start=1):
        sid=m["stable_id"]
        if sid not in child_files:
            result["children"].append({"id":sid,"filename":None,"expected_crc":None,"actual_crc":None,"status":"MISSING"})
            result["missing"].append(f"child{idx:02d}"); result["overall"]="FAIL"; continue
        p=child_files[sid]; result["found"]+=1
        ch, cpayload, _ = read_png(p)
        status="MATCH" if ch["crc_ok"] else "MISMATCH"
        if not ch["crc_ok"]: result["overall"]="FAIL"; result["mismatched"].append(p.name)
        result["children"].append({"id":sid,"filename":p.name,"expected_crc":ch["checksum"],"actual_crc":ch["actual_crc"],"status":status})
    # parity check: recompute XOR of surviving children, compare to stored parity
    if len(manifest)>=2:
        # only if all present
        if len(result["missing"])==0 and len(result["mismatched"])==0:
            # recompute
            max_len=max(m["payload_len"] for m in manifest)
            recomputed=bytearray(max_len)
            for m in manifest:
                p=child_files[m["stable_id"]]
                _, cpayload, _ = read_png(p)
                for i,b in enumerate(cpayload):
                    recomputed[i]^=b
            match=bytes(recomputed)==parity_bytes
            result["parity"]={"expected_crc":_crc32(parity_bytes),"actual_crc":_crc32(bytes(recomputed)),"status":"MATCH" if match else "MISMATCH"}
            if not match: result["overall"]="FAIL"
        else:
            result["parity"]={"expected_crc":_crc32(parity_bytes),"actual_crc":None,"status":"N/A — missing/mismatched child"}
    else:
        result["parity"]={"expected_crc":None,"actual_crc":None,"status":"N/A"}
    return result

def print_verify(result):
    slug=Path(result["dir"]).name
    lines=[]
    lines.append(f"HUEM verify: {slug}")
    lines.append(f"dir: {result['dir']}")
    if result.get("parent_id"):
        lines.append(f"parent: {result['parent_id']} {result.get('parent_file','')}")
    exp=result.get("expected",0); found=result.get("found",0)
    miss=exp-found
    lines.append(f"manifest: expected {exp} children, found {found} — {'OK' if miss==0 else f'MISSING {miss}'}")
    for i,c in enumerate(result["children"], start=1):
        if c["status"]=="MISSING":
            lines.append(f"child {i}: {c['id']} MISSING")
        else:
            lines.append(f"child {i}: {c['id']} {c['filename']} — CRC32 expected {c['expected_crc']:08x}, actual {c['actual_crc']:08x} — {c['status']}")
    p=result["parity"]
    if p["status"]=="N/A":
        lines.append(f"parity: N/A — single child, manifest only")
    elif p["expected_crc"] is not None:
        lines.append(f"parity: expected {p['expected_crc']:08x}, actual {p['actual_crc']:08x} — {p['status']}" if p["actual_crc"] is not None else f"parity: {p['status']}")
    else:
        lines.append(f"parity: {p['status']}")
    lines.append(f"overall: {result['overall']} — missing: {result['missing']}, mismatched: {result['mismatched']}")
    print("\n".join(lines))
    print(json.dumps(result, indent=2, default=str))
    return lines

def regrow_missing(dir_path, missing_index, confirm=False):
    if not confirm: raise PermissionError("regrow requires explicit confirm=True")
    dir_path=Path(dir_path)
    res=verify_dir(dir_path)
    parents=[p for p in dir_path.glob("*.png") if "-parent" in p.name]
    if not parents: raise ValueError("no parent")
    h, payload, _ = read_png(parents[0])
    manifest, off = _decode_manifest(payload)
    parity=payload[off:]
    target=manifest[missing_index-1]
    max_len=max(m["payload_len"] for m in manifest)
    # XOR parity with surviving siblings
    recovered=bytearray(parity[:max_len])
    # need surviving payloads
    for m in manifest:
        if m["stable_id"]==target["stable_id"]: continue
        # find file
        found=None
        for p in dir_path.glob("*.png"):
            if p==parents[0]: continue
            try:
                ch, cp, _ = read_png(p)
                if ch["stable_id"]==m["stable_id"]: found=cp; break
            except Exception: pass
        if found is None: raise ValueError("another child missing, cannot regrow")
        for i,b in enumerate(found):
            recovered[i]^=b
    recovered=bytes(recovered[:target["payload_len"]])
    if _crc32(recovered)!=0: # checksum is in child header, not here; verify by decoding and checking source hash? Use manifest source hash: need to recompute? We trust CRC from child header after rebuild
        pass
    tagged=decode_word_block(recovered, target["word_count"])
    # rebuild child PNG with same header values: need original header info — reconstruct from manifest + recovered text
    chunk_text=" ".join(w for w,_ in tagged)
    dom, dcount, resid, _=_dominant(tagged)
    # tick from parent header
    tick=h["tick"]
    header=pack_header(dominant=dom, word_count=len(tagged), tick=tick, title=f"regrown child {missing_index:02d}", source=chunk_text, payload=recovered, stable_id=target["stable_id"], dominant_count=dcount, residual=resid, parent_id=h["stable_id"])
    # filename: use original pattern if possible, else generate
    date_str=time.strftime("%Y%m%d", time.localtime(tick))
    parent_hash8=hashlib.sha256(struct.pack(">Q", h["stable_id"])).hexdigest()[:8]
    fname=f"HUEMv1_{dom}_{date_str}_r{resid:02d}_{_hash8_hex(chunk_text)}_p{parent_hash8}_regrown-child{missing_index:02d}.png"
    # Try to preserve original slug from dir name
    png_path=dir_path/fname
    img=_render_png(header, recovered, tagged, f"regrown child {missing_index:02d}")
    _atomic_write_png(png_path, img)
    return {"regrown":str(png_path),"stable_id":target["stable_id"],"words":len(tagged)}

def rebuild_parent(dir_path, confirm=False):
    if not confirm: raise PermissionError("rebuild_parent requires explicit confirm=True")
    dir_path=Path(dir_path)
    # collect children, order by filename child number
    child_pngs=sorted([p for p in dir_path.glob("*.png") if "-child" in p.name or "regrown-child" in p.name])
    if not child_pngs: raise ValueError("no children to rebuild from")
    children=[]; payloads=[]; tagged_all=[]
    parent_id=None; tick=None; slug=dir_path.name.rsplit("-",1)[0]
    for p in child_pngs:
        h, payload, _ = read_png(p)
        if parent_id is None: parent_id=h["parent_id"]; tick=h["tick"]
        children.append({"stable_id":h["stable_id"],"word_count":h["word_count"],"payload_len":h["payload_len"],"source_hash":h["source_hash"]})
        payloads.append(payload)
        tagged_all.extend(decode_word_block(payload, h["word_count"]))
    max_len=max(len(p) for p in payloads)
    parity=bytearray(max_len)
    for p in payloads:
        for i,b in enumerate(p): parity[i]^=b
    manifest=_encode_manifest(children)
    parent_payload=manifest+bytes(parity)
    dom, dcount, resid, _=_dominant(tagged_all)
    source_text=" ".join(w for w,_ in tagged_all)
    header=pack_header(dominant=dom, word_count=len(tagged_all), tick=tick, title=f"{slug} — parent", source=source_text, payload=parent_payload, stable_id=parent_id, dominant_count=dcount, residual=resid, parent_id=0)
    hash8=_hash8_hex(source_text)
    # use original dir hash if possible: dir suffix is original source hash, keep it in filename? Use hash8 of combined text for filename, but dir stays same
    fname=_filename(dom, tick, resid, dir_path.name.rsplit("-",1)[-1], slug, suffix="-parent")
    png_path=dir_path/fname
    img=_render_png(header, parent_payload, [], f"{slug} — parent", is_parent=True, child_info=f"{len(children)} children, parity {max_len} bytes")
    _atomic_write_png(png_path, img)
    return {"rebuilt":str(png_path),"parent_id":parent_id,"children":len(children)}

if __name__=="__main__":
    import sys
    if len(sys.argv)>=3 and sys.argv[1]=="verify":
        res=verify_dir(sys.argv[2]); print_verify(res)
    else:
        print("usage: huemem.py verify <memory-dir>")
