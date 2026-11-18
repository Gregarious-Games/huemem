"""Accelerator verification — header fast path, search equivalence, fault boundary.

Setup: the frozen golden set is stored in the private CADE format, so the
test first re-encodes it as HUEM (public format) into /tmp/accel-test/huemem,
verifying the word lists are identical, and injects the distinguishing /
plain sentences into the .md sidecars (as the Round 24 parity check did).
Then:
1. read_header_fast == read_png header on every HUEM PNG.
2. scan_headers timing vs full load timing.
3. AccelIndex.search top-1 == RecallIndex.search top-1 on all 43 paraphrase
   + 10 exact + 9 two-turn prompts (same synonym map, same gate, same RRF).
4. fault_report on synthetic cases: intact split, 1 child lost, parent
   lost, 2 children lost, parent+child lost, standalone intact / PNG lost /
   PNG corrupt / PNG+sidecar lost.

All test data is synthetic or the golden set re-encoded (no personal details
beyond the golden set itself, which stays in /tmp). Run with the venv python.
"""
import ast, json, os, shutil, sys, time
sys.path.insert(0, os.path.expanduser('~/workspace/huemem'))
sys.path.insert(0, os.path.expanduser('~/workspace/cade-png-memory'))
sys.path.insert(0, os.path.expanduser('~/workspace/cade-png-memory/recall-test/real'))
from huemem import read_png, decode_payload, write_standalone, write_split
from huemem_accel import read_header_fast, scan_headers, build_index, AccelIndex, fault_report
from huemem_recall import RecallIndex, load_memories
from cade_v2 import read_png as cade_read_png, decode_payload as cade_decode

GOLD = os.path.expanduser('~/workspace/cade-png-memory/recall-test/golden')
CADE_MEM = os.path.join(GOLD, 'memories')
TMP = '/tmp/accel-test'
HUEM_MEM = os.path.join(TMP, 'huemem')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(HUEM_MEM, exist_ok=True)


def get_dict(fname, name):
    tree = ast.parse(open(os.path.join(GOLD, fname)).read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == name:
            return ast.literal_eval(node.value)
    raise KeyError(name)
DIST = get_dict('write_time_test.py', 'DIST')
PLAIN = get_dict('plain_test.py', 'PLAIN')

# --- 0. CADE -> HUEM conversion, word lists verified identical ---
converted, word_mismatch = 0, 0
for d in sorted(os.listdir(CADE_MEM)):
    dpath = os.path.join(CADE_MEM, d)
    slug = d.rsplit('-', 1)[0]
    png = [f for f in os.listdir(dpath) if f.endswith('.png')][0]
    md = [f for f in os.listdir(dpath) if f.endswith('.md')][0]
    h, payload, _ = cade_read_png(os.path.join(dpath, png))
    tagged = cade_decode(h, payload)
    src = ''
    for line in open(os.path.join(dpath, md), encoding='utf-8', errors='replace'):
        if line.startswith('- source: '):
            src = line[len('- source: '):].strip()
    res = write_standalone(src, slug, slug, HUEM_MEM)
    h2, payload2, _ = read_png(res['png'])
    words2 = [w for w, _ in decode_payload(h2, payload2)]
    if [w for w, _ in tagged] != words2:
        word_mismatch += 1
        print(f'  WORD MISMATCH {slug}')
    with open(res['md'], 'a', encoding='utf-8') as f:
        f.write(f'- distinguishing: {DIST[slug]}\n- plain: {PLAIN[slug]}\n')
    converted += 1
print(f'0. CADE->HUEM conversion: {converted} memories, '
      f'word-list mismatches: {word_mismatch}', flush=True)

# --- 1. Header fast path ---
mismatch, total, filter_types = 0, 0, {}
for d in sorted(os.listdir(HUEM_MEM)):
    dpath = os.path.join(HUEM_MEM, d)
    for f in sorted(os.listdir(dpath)):
        if not f.endswith('.png'):
            continue
        total += 1
        h_full, _, _ = read_png(os.path.join(dpath, f))
        h_fast, st = read_header_fast(os.path.join(dpath, f))
        filter_types[st['filter']] = filter_types.get(st['filter'], 0) + 1
        for k in ('magic', 'version', 'dominant', 'word_count', 'tick',
                  'title_hash', 'source_hash', 'payload_len', 'checksum',
                  'stable_id', 'dominant_count', 'residual', 'flags', 'parent_id'):
            if h_full[k] != h_fast[k]:
                mismatch += 1
                print(f'  MISMATCH {d}/{f} field {k}: full={h_full[k]} fast={h_fast[k]}')
print(f'1. header fast path: {total - mismatch}/{total} fields match '
      f'({total} PNGs, filter types {filter_types})', flush=True)

# --- 2. Scan timing ---
t0 = time.time()
scan = scan_headers(HUEM_MEM)
t_scan = time.time() - t0
t0 = time.time()
mems = load_memories(HUEM_MEM)
t_full = time.time() - t0
print(f'2. scan_headers: {scan["count"]} PNGs in {t_scan:.3f}s '
      f'({1000 * t_scan / max(scan["count"], 1):.2f} ms/PNG); '
      f'full load_memories: {t_full:.3f}s '
      f'({t_full / max(t_scan, 1e-9):.1f}x slower)', flush=True)

# --- 3. Search equivalence ---
EXP = os.path.expanduser('~/workspace/cade-png-memory/recall-test/expanded')
para = [(r['prompt'], r['expected']) for r in
        json.load(open(os.path.join(EXP, 'expanded-results.json')))['para_fixed']]
p0 = json.load(open(os.path.join(GOLD, 'p0-results.json')))
p0b = json.load(open(os.path.join(GOLD, 'p0b-results.json')))
para += [(r['prompt'], r['expected']) for r in p0['results']['held_paraphrase']]
para += [(r['prompt'], r['expected']) for r in p0b['held_paraphrase']]
real = json.load(open(os.path.expanduser(
    '~/workspace/cade-png-memory/recall-test/real/real-results.json')))
exact = [(r['prompt'], r['expected']) for r in real['exact']]
two = [(r['prompt1'], r['prompt2'], r['expected'])
       for r in p0['results']['held_two_turn']]
two += [(r['prompt1'], r['prompt2'], r['expected'])
        for r in p0b['held_two_turn']]
print('3. building accel index...', flush=True)
idx_dir = os.path.join(TMP, 'index')
build_index(HUEM_MEM, idx_dir)
accel = AccelIndex(idx_dir)
print('   building recall index...', flush=True)
recall = RecallIndex(mems)
diffs = []
for prompt, expected in para + exact:
    a = accel.search(prompt, top=1)
    b = recall.search(prompt, top=1)
    if (a[0][0] if a else None) != (b[0][0] if b else None):
        diffs.append((prompt[:60], a[0][0] if a else None, b[0][0] if b else None))
accel_para = sum(1 for p, e in para if (accel.search(p, top=1) or [(None,)])[0][0] == e)
accel_exact = sum(1 for p, e in exact if (accel.search(p, top=1) or [(None,)])[0][0] == e)
t1 = t2 = 0
for p1, p2, e in two:
    if (accel.search(p1, top=1) or [(None,)])[0][0] == e:
        t1 += 1
    src = accel.index['sources'][e]
    if (accel.search(p2 + ' ' + src, top=1) or [(None,)])[0][0] == e:
        t2 += 1
rt1 = rt2 = 0
for p1, p2, e in two:
    if (recall.search(p1, top=1) or [(None,)])[0][0] == e:
        rt1 += 1
    src = mems[e]['source']
    if (recall.search(p2 + ' ' + src, top=1) or [(None,)])[0][0] == e:
        rt2 += 1
print(f'   accel vs recall top-1 diffs: {len(diffs)}/53', flush=True)
for d in diffs:
    print(f'     {d}', flush=True)
print(f'   accel scores: paraphrase {accel_para}/43, exact {accel_exact}/10, '
      f'two-turn t1 {t1}/9 t2 {t2}/9 (H expects 39/43, 10/10, 9/9, 9/9)', flush=True)
print(f'   recall scores: two-turn t1 {rt1}/9 t2 {rt2}/9 '
      f'(same generic synonym map — t2 8/9 here vs 9/9 in the harness is the map, not the accel)', flush=True)

# --- 4. Fault boundary (synthetic) ---
print('4. fault_report cases...', flush=True)
LONG = (' '.join(f'The warehouse unit {i} stores part number {1000 + i} '
                 f'beside crate {i % 7} on shelf {i % 5}.' for i in range(120)))
root = os.path.join(TMP, 'fault')
cases = {}


def split_case(name, drop_children=(), drop_parent=False):
    d = write_split(LONG, 'fault-' + name, 'Fault ' + name, root)
    if drop_parent:
        os.remove(d['parent'])
    for i in drop_children:
        os.remove(d['children'][i]['path'])
    return d['dir']


cases['intact split'] = (split_case('intact'), 'none', True)
cases['1 child lost'] = (split_case('onechild', drop_children=(0,)), 'regrow_missing', True)
cases['parent lost'] = (split_case('parent', drop_parent=True), 'rebuild_parent', True)
cases['2 children lost'] = (split_case('twochild', drop_children=(0, 1)), None, False)
cases['parent+child lost'] = (split_case('both', drop_children=(0,), drop_parent=True), None, False)
s = write_standalone('The depot gate code is 4417 and the key is under the blue bin.',
                     'fault-standalone', 'Fault standalone', root)
cases['standalone intact'] = (s['dir'], 'none', True)
s2 = write_standalone('The depot gate code is 4417 and the key is under the blue bin.',
                      'fault-lost', 'Fault lost', root)
os.remove(s2['png'])
cases['standalone PNG lost'] = (s2['dir'], 'rewrite_from_sidecar', True)
s3 = write_standalone('The depot gate code is 4417 and the key is under the blue bin.',
                      'fault-corrupt', 'Fault corrupt', root)
with open(s3['png'], 'r+b') as f:
    f.seek(5000)
    b = f.read(1)
    f.seek(5000)
    f.write(bytes([b[0] ^ 0xFF]))
cases['standalone PNG corrupt'] = (s3['dir'], 'rewrite_from_sidecar', True)
s4 = write_standalone('The depot gate code is 4417 and the key is under the blue bin.',
                      'fault-gone', 'Fault gone', root)
os.remove(s4['png'])
os.remove(s4['md'])
cases['standalone PNG+sidecar lost'] = (s4['dir'], None, False)
ok = True
for name, (d, want_action, want_rec) in cases.items():
    r = fault_report(d)
    good = (r['action'] == want_action and r['recoverable'] == want_rec)
    ok &= good
    print(f"   {name}: action={r['action']} recoverable={r['recoverable']} "
          f"{'OK' if good else 'FAIL — expected action=' + str(want_action) + ' rec=' + str(want_rec)}", flush=True)
print(f'4. fault boundary: {"ALL PASS" if ok else "FAILURES ABOVE"}', flush=True)

# --- 5. Left's review fixes (findings 1, 2, 3, 4, 8, 9) ---
print('5. Left-review fixes...', flush=True)
from huemem import load_memory_words, verify_dir, rebuild_parent, tag_text
from PIL import Image
ok5 = True


def check5(name, cond, extra=''):
    global ok5
    ok5 &= bool(cond)
    print(f"   {name}: {'OK' if cond else 'FAIL'} {extra}", flush=True)


# Finding 1: slug containing '-parent' must not break verification.
d = write_split(LONG, 'my-parent-test', 'Parent slug test', os.path.join(TMP, 'left1'))
r = fault_report(d['dir'])
check5('parent-slug split intact', r['action'] == 'none' and r['recoverable'])
r = verify_dir(d['dir'])
check5('parent-slug verify_dir PASS', r['overall'] == 'PASS', f"overall={r['overall']}")

# Finding 2: truncated parent must report, not raise.
d = write_split(LONG, 'trunc-parent', 'Trunc parent', os.path.join(TMP, 'left2'))
with open(d['parent'], 'r+b') as f:
    f.truncate(100)
try:
    r = fault_report(d['dir'])
    check5('truncated parent reports rebuild', r['action'] == 'rebuild_parent' and r['recoverable'], f"action={r['action']}")
except Exception as e:
    check5('truncated parent reports rebuild', False, f'raised {type(e).__name__}: {e}')

# Finding 3: manifest-region corruption -> rebuild from disk children.
d = write_split(LONG, 'manifest-corrupt', 'Manifest corrupt', os.path.join(TMP, 'left3'))
pp = d['parent']
im = Image.open(pp); im.load()
w, h = im.size
px = list(im.getdata())
raw = bytearray()
for p in px[:w]:
    raw.extend(p[:3])
raw[50] ^= 0xFF  # payload byte 2: inside child 1's manifest entry
img = Image.new('RGB', (w, h))
img.putdata([tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)][:w] + list(px[w:]))
img.save(pp, format='PNG')
r = fault_report(d['dir'])
check5('manifest corrupt -> rebuild', r['action'] == 'rebuild_parent' and r['recoverable'], f"action={r['action']} rec={r['recoverable']}")

# Finding 4: split-aware loading — full word list, not one child/parent.
root4 = os.path.join(TMP, 'left4')
d = write_split(LONG, 'loader-split', 'Loader split', root4)
mems4 = load_memories(root4)
want = [w for w, _ in tag_text(LONG)]
got = mems4['loader-split']['words']
check5('split loader full words', got == want, f'{len(got)} vs {len(want)} words')
idx4 = build_index(root4, os.path.join(TMP, 'index4'), lexical_only=True)
check5('split index full words', idx4['words']['loader-split'] == want)

# Finding 8: 1-memory corpus must not crash search (gate's sc[1]).
# Exercise the gate path directly with stubbed 1-row embeddings (no
# model load — the guard is what is under test, not the encoder).
root8 = os.path.join(TMP, 'left8')
write_standalone('The depot gate code is 4417.', 'only-one', 'Only one', root8)
import numpy as np


class FakeEnc:
    def encode(self, texts, normalize_embeddings=True):
        return np.ones((len(texts), 4), dtype=np.float32)


idx8 = build_index(root8, os.path.join(TMP, 'index8'), lexical_only=True)
acc8 = AccelIndex(os.path.join(TMP, 'index8'))
acc8.emb = {n: np.ones((1, 4), dtype=np.float32) for n in ('word', 'dist', 'plain')}
acc8.encoder = FakeEnc()
rec8 = RecallIndex(load_memories(root8), dense_model=None)
rec8.np = np
rec8.encoder = FakeEnc()
rec8.emb_word = {'only-one': np.ones(4, dtype=np.float32)}
rec8.emb_dist = {'only-one': np.ones(4, dtype=np.float32)}
rec8.emb_plain = {'only-one': np.ones(4, dtype=np.float32)}
rec8.has_plain = True
try:
    h8a = acc8.search('depot gate code', top=1)
    h8r = rec8.search('depot gate code', top=1)
    check5('1-memory search no crash', h8a and h8a[0][0] == 'only-one' and h8r and h8r[0][0] == 'only-one')
except Exception as e:
    check5('1-memory search no crash', False, f'raised {type(e).__name__}: {e}')

# Finding 9: rebuild_parent with >=10 children keeps word order.
LONG11 = ' '.join(f'The warehouse unit {i} stores part number {1000 + i} '
                  f'beside crate {i % 7} on shelf {i % 5}.' for i in range(200))
root9 = os.path.join(TMP, 'left9')
d = write_split(LONG11, 'many-children', 'Many children', root9)
n_children = len(d['children'])
os.remove(d['parent'])
rebuilt = rebuild_parent(d['dir'], confirm=True)
res9 = verify_dir(d['dir'])
tagged9 = [w for w, _ in load_memory_words(d['dir'])[0]]
want9 = [w for w, _ in tag_text(LONG11)]
check5('rebuild >=10 children', n_children >= 10 and res9['overall'] == 'PASS' and tagged9 == want9,
       f'children={n_children} verify={res9["overall"]} words={len(tagged9)}/{len(want9)}')
print(f'5. Left-review fixes: {"ALL PASS" if ok5 else "FAILURES ABOVE"}', flush=True)
print('done', flush=True)
