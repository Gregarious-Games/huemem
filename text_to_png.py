#!/usr/bin/env python3
"""
HueMem System v1
Text -> RGB categories -> PNG layer + .md memory sidecar

Palette (spaCy categories):
 PERSON (255,99,71) Tomato Red
 GPE (46,139,87) Sea Green, LOC (60,179,113) Medium Sea Green
 ORG (70,130,180) Steel Blue
 PRODUCT (218,165,32) Goldenrod
 NOUN (100,149,237) Cornflower Blue, PROPN (65,105,225) Royal Blue
 VERB (255,165,0) Orange
 ADJ (186,85,211) Medium Orchid, ADV (221,160,221) Plum
 OTHER (169,169,169) Dark Gray

Uses spaCy if available, otherwise a fallback tagger
(small example lists + simple heuristics).
Each run writes:
  <slug>.png  - color strip, one block per word, hue = category
  <slug>.md   - memory file: summary, counts, dominant colors, source text
"""
import json, re, sys
from pathlib import Path
from collections import Counter

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise SystemExit("Pillow required")

CATEGORY_TO_RGB = {
    "PERSON": (255, 99, 71),
    "GPE": (46, 139, 87),
    "LOC": (60, 179, 113),
    "ORG": (70, 130, 180),
    "PRODUCT": (218, 165, 32),
    "NOUN": (100, 149, 237),
    "PROPN": (65, 105, 225),
    "VERB": (255, 165, 0),
    "ADJ": (186, 85, 211),
    "ADV": (221, 160, 221),
    "OTHER": (169, 169, 169),
}

# Fallback knowledge: example names only — add your own, or install spaCy
PEOPLE = {"ada", "lovelace"}
PLACES = {"london"}
ORGS = set()  # handled as place/org context below
VERBS = {"wrote", "installed", "installing", "sold", "deleted", "went", "got", "met", "began", "started", "help", "put", "build", "learn", "understand", "speak", "talk", "walked", "created"}
ADJS = {"early", "great", "new", "live", "small", "very", "low", "professional"}
ADVS = {"yesterday", "today", "now", "first", "again", "directly"}

def fallback_tag(text):
    tokens = re.findall(r"[A-Za-z0-9']+|[.,!?;]", text)
    out = []
    for tok in tokens:
        low = tok.lower()
        if tok in ".,!?;":
            out.append((tok, "OTHER"))
        elif low in PEOPLE:
            out.append((tok, "PERSON"))
        elif low in PLACES:
            out.append((tok, "GPE"))
        elif low in VERBS:
            out.append((tok, "VERB"))
        elif low in ADJS:
            out.append((tok, "ADJ"))
        elif low in ADVS:
            out.append((tok, "ADV"))
        elif tok[0].isupper():
            out.append((tok, "PROPN"))
        elif low.endswith("ing") or low.endswith("ed"):
            out.append((tok, "VERB"))
        else:
            out.append((tok, "NOUN"))
    return out

def spacy_tag(text):
    import spacy
    nlp = spacy.load("en_core_web_sm")
    doc = nlp(text)
    out = []
    for token in doc:
        if token.ent_type_ in CATEGORY_TO_RGB:
            cat = token.ent_type_
        elif token.pos_ in CATEGORY_TO_RGB:
            cat = token.pos_
        else:
            cat = "OTHER"
        out.append((token.text, cat))
    return out

def tag_text(text):
    try:
        return spacy_tag(text)
    except Exception as e:
        print(f"[huemem] spaCy not available ({e}), using fallback tagger", file=sys.stderr)
        return fallback_tag(text)

def render_png(tagged, out_path, title=""):
    # one block per word, width by word length, height 120, label below
    block_h = 120
    label_h = 60
    W = max(600, sum(max(40, len(w)*14+20) for w, _ in tagged))
    H = block_h + label_h + (40 if title else 0)
    img = Image.new("RGB", (W, H), (18, 18, 22))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except Exception:
        font = ImageFont.load_default()
        small = font
    y = 40 if title else 0
    if title:
        d.text((20, 8), title, fill=(255,255,255), font=small)
    x = 0
    for word, cat in tagged:
        rgb = CATEGORY_TO_RGB.get(cat, (169,169,169))
        w = max(40, len(word)*14+20)
        d.rectangle([x, y, x+w-2, y+block_h], fill=rgb)
        # word label under block, rotated? keep horizontal, truncate
        d.text((x+4, y+block_h+6), word[:12], fill=(255,255,255), font=small)
        d.text((x+4, y+block_h+30), cat, fill=rgb, font=small)
        x += w
    img.save(out_path)
    return out_path

def write_memory(tagged, text, out_md, png_name, title=""):
    counts = Counter(cat for _, cat in tagged)
    total = len(tagged)
    dominant = counts.most_common(3)
    lines = []
    lines.append(f"# PNG Memory — {title or png_name}")
    lines.append("")
    lines.append(f"- PNG: `{png_name}`")
    lines.append(f"- Source text: {text}")
    lines.append(f"- Words: {total}")
    lines.append(f"- Dominant categories: {', '.join(f'{c} {n} ({n/total*100:.0f}%)' for c, n in dominant)}")
    lines.append("")
    lines.append("## Color summary (phase = dominant hue)")
    for cat, n in counts.most_common():
        rgb = CATEGORY_TO_RGB[cat]
        lines.append(f"- {cat} RGB{rgb} — {n}")
    lines.append("")
    lines.append("## Word -> Category -> RGB")
    for word, cat in tagged:
        lines.append(f"- {word} | {cat} | RGB{CATEGORY_TO_RGB[cat]}")
    Path(out_md).write_text("\n".join(lines) + "\n")
    return out_md

def build(text, slug, out_dir, title=""):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tagged = tag_text(text)
    png = out_dir / f"{slug}.png"
    md = out_dir / f"{slug}.md"
    render_png(tagged, png, title=title)
    write_memory(tagged, text, md, png.name, title=title)
    print(json.dumps({"slug": slug, "png": str(png), "md": str(md),
                      "words": len(tagged),
                      "counts": dict(Counter(c for _, c in tagged))}, indent=2))
    return png, md

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: text_to_png.py <slug> <text> [out_dir] [title]")
        sys.exit(1)
    slug = sys.argv[1]
    text = sys.argv[2]
    out_dir = sys.argv[3] if len(sys.argv) > 3 else "layers"
    title = sys.argv[4] if len(sys.argv) > 4 else slug
    build(text, slug, out_dir, title)
