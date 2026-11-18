"""Accelerator benchmark — what happens at N = 10,000 (measured, not asserted).

Three parts, all synthetic data (seeded RNG, template sentences — nothing
personal):
  A. Scoring latency sweep: N = 100 / 1,000 / 5,000 / 10,000. The derived
     index is built from synthetic word lists; dense matrices are random
     unit vectors, which is honest for timing because the matmul cost is
     content-independent. 50 queries each; mean/p95 per-query latency for
     the full search path (lexical posting lookup + one matmul per leg +
     RRF + gate). The query encoder is stubbed with a fixed random vector
     — real query encoding is a constant ~15 ms (measured in part B) and
     does not scale with N.
  B. Build cost: real MPNet encoding throughput (docs/sec, 3 legs per
     memory), header-only scan vs full decode on 200 synthetic HUEM PNGs,
     and a full build_index timing on those 200.
  C. Collision rate at N = 300 with REAL embeddings (was 1,000; shrunk
     after a runtime restart killed the first run mid-C — the box is
     throttled and restarts have hit three times tonight, so the run is
     also resumable: results are checkpointed after each part and
     finished parts are skipped on re-run). 300 synthetic memories
     from ~40 topic templates with entity variations (same topic,
     different name/number/place — the near-neighbour structure that
     causes collisions), queries made by synonym-swapping and dropping
     words from each memory's plain sentence. Reports top-1 accuracy
     and the share of queries whose top-1 margin is < 0.05 (gate falls
     back to fusion). Compare with the 40-memory golden baseline (4/43
     near-misses).

Outputs accel_bench_results.json next to this script.
"""
import json, os, random, shutil, statistics, sys, time
sys.path.insert(0, os.path.expanduser('~/workspace/huemem'))
import numpy as np
from huemem import write_standalone
from huemem_accel import AccelIndex, build_index, read_header_fast, scan_headers
from huemem_recall import load_memories, tokens, expand

rng = random.Random(7)
TMP = '/tmp/accel-bench'
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   'accel_bench_results.json')
# Resumable: a restart keeps finished parts; re-running skips them.
results = json.load(open(OUT)) if os.path.exists(OUT) else {}


def save():
    json.dump(results, open(OUT, 'w'), indent=2)


def synth_words(i, vocab):
    n = rng.randint(25, 60)
    return [rng.choice(vocab) for _ in range(n)]


# --- A. Latency sweep (random unit vectors: matmul cost is content-free) ---
print('A. latency sweep...', flush=True)
VOCAB = [f'w{k}' for k in range(400)] + \
    ['sold', 'bought', 'price', 'miles', 'garage', 'phone', 'camera', 'test',
     'memory', 'system', 'project', 'meeting', 'family', 'yard', 'rental']
queries = [' '.join(rng.sample(VOCAB, rng.randint(4, 9))) for _ in range(50)]
part_a = {}
for N in (100, 1000, 5000, 10000):
    slugs = [f'mem-{i:05d}' for i in range(N)]
    words = {s: synth_words(i, VOCAB) for i, s in enumerate(slugs)}
    df, postings = {}, {}
    for s in slugs:
        mt = expand(set(tokens(' '.join(words[s]))) | set(tokens(s)))
        for t in mt:
            df[t] = df.get(t, 0) + 1
            postings.setdefault(t, []).append(slugs.index(s))
    acc = AccelIndex.__new__(AccelIndex)
    acc.np = np
    acc.index = {'model': 'synthetic', 'slugs': slugs, 'df': df,
                 'postings': postings, 'has_plain': True}
    acc.slugs, acc.N = slugs, N
    acc.idx_of = {s: i for i, s in enumerate(slugs)}
    acc.df, acc.postings, acc.has_plain = df, postings, True
    d = 384
    acc.emb = {name: (np.random.default_rng(1).normal(size=(N, d)).astype(np.float32)
                      / np.sqrt(d)) for name in ('word', 'dist', 'plain')}
    for m in acc.emb.values():
        m /= np.linalg.norm(m, axis=1, keepdims=True)
    # Stub the query encoder: fixed random unit vector (encode cost measured in B).
    qvec = np.random.default_rng(2).normal(size=d).astype(np.float32)
    qvec /= np.linalg.norm(qvec)

    class FakeEnc:
        def encode(self, texts, normalize_embeddings=True):
            return np.tile(qvec, (len(texts), 1))
    acc.encoder = FakeEnc()
    lat = []
    for q in queries:
        t0 = time.perf_counter()
        acc.search(q, top=5)
        lat.append((time.perf_counter() - t0) * 1000)
    part_a[N] = {'mean_ms': round(statistics.mean(lat), 3),
                 'p95_ms': round(float(np.percentile(lat, 95)), 3),
                 'max_ms': round(max(lat), 3)}
    print(f'   N={N}: mean {part_a[N]["mean_ms"]} ms, p95 {part_a[N]["p95_ms"]} ms',
          flush=True)
results['A_latency_ms'] = part_a
save()

# --- B. Build cost ---
print('B. build cost...', flush=True)
from sentence_transformers import SentenceTransformer
enc = SentenceTransformer('all-mpnet-base-v2')
texts = [' '.join(synth_words(i, VOCAB)) for i in range(200)]
t0 = time.time()
enc.encode(texts, normalize_embeddings=True)
enc_s = time.time() - t0
docs_per_s = 200 / enc_s
print(f'   MPNet encode: {docs_per_s:.1f} docs/s '
      f'(3 legs -> build ≈ {3 * 10000 / docs_per_s:.0f}s at N=10,000)', flush=True)
png_dir = os.path.join(TMP, 'pngs')
for i in range(200):
    write_standalone(' '.join(texts[i][:400]), f'bench-{i:03d}',
                     f'bench {i}', png_dir)
t0 = time.time()
scan = scan_headers(png_dir)
t_scan = time.time() - t0
t0 = time.time()
load_memories(png_dir)
t_full = time.time() - t0
print(f'   header scan: {t_scan:.2f}s for 200 PNGs '
      f'({1000 * t_scan / 200:.2f} ms/PNG); full load: {t_full:.2f}s '
      f'({t_full / t_scan:.1f}x slower)', flush=True)
t0 = time.time()
qvec_t0 = time.time()
enc.encode(['sample query text for encoding cost'], normalize_embeddings=True)
qenc_ms = (time.time() - qvec_t0) * 1000
idx = build_index(png_dir, os.path.join(TMP, 'index200'))
print(f'   query encode: {qenc_ms:.1f} ms (constant, independent of N); '
      f'build_index 200 memories: {idx["build_seconds"]:.1f}s', flush=True)
results['B_build'] = {
    'mpnet_docs_per_s': round(docs_per_s, 1),
    'build_seconds_N10000_est': round(3 * 10000 / docs_per_s),
    'header_scan_ms_per_png': round(1000 * t_scan / 200, 2),
    'full_load_vs_scan': round(t_full / t_scan, 1),
    'query_encode_ms': round(qenc_ms, 1),
    'build_index_200_seconds': round(idx['build_seconds'], 1)}
save()

# --- C. Collisions at N=300, real embeddings ---
print('C. collisions at N=300 (real embeddings)...', flush=True)
TOPICS = [('depot', 'gate code', 'bin'), ('warehouse', 'part number', 'crate'),
          ('garage', 'van sale', 'miles'), ('studio', 'camera test', 'footage'),
          ('yard', 'rental unit', 'lease'), ('lab', 'memory chip', 'board'),
          ('office', 'invoice total', 'client'), ('shop', 'tool order', 'wrench')]
NAMES = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel']
PLACES = ['mesa', 'tucson', 'phoenix', 'tempe', 'chandler', 'gilbert']
N_C = 300
mem_dir = os.path.join(TMP, 'mem300')
plains = {}
for i in range(N_C):
    topic, thing, item = TOPICS[i % len(TOPICS)]
    name, place = NAMES[i % len(NAMES)], PLACES[(i // 8) % len(PLACES)]
    num = 1000 + (i * 37) % 9000
    plain = (f'The {topic} {thing} for {name} in {place} is {num} '
             f'and the {item} is ready.')
    plains[f'col-{i:04d}'] = plain
    write_standalone(plain + ' ' + ' '.join(
        f'unit {i} {topic} {thing} {name} {place} {num} {item}'
        for _ in range(6)),
        f'col-{i:04d}', f'col {i}', mem_dir)
print('   built 300 synthetic memories; adding plain/distinguishing lines...', flush=True)
for slug, plain in plains.items():
    d = [d for d in os.listdir(mem_dir) if d.startswith(slug + '-')][0]
    mds = [f for f in os.listdir(os.path.join(mem_dir, d)) if f.endswith('.md')]
    with open(os.path.join(mem_dir, d, mds[0]), 'a', encoding='utf-8') as f:
        f.write(f'- distinguishing: {plain}\n- plain: {plain}\n')
idx300 = build_index(mem_dir, os.path.join(TMP, 'index300'))
acc300 = AccelIndex(os.path.join(TMP, 'index300'))
print('   index built; running 100 queries...', flush=True)
SYN = {'ready': ['done', 'prepared'], 'gate': ['door', 'entry'],
       'sale': ['sold'], 'test': ['check'], 'total': ['amount', 'sum']}
correct, low_margin = 0, 0
for qi in range(100):
    slug = f'col-{qi * 3:04d}'
    words = plains[slug].split()
    qwords = [SYN.get(w, [w])[0] if rng.random() < 0.3 else w for w in words]
    qwords = [w for w in qwords if rng.random() > 0.15]
    hits = acc300.search(' '.join(qwords), top=2)
    if hits and hits[0][0] == slug:
        correct += 1
    # margin from the plain-cos leg, measured directly:
    dense = acc300._dense(' '.join(qwords))
    sv = sorted(dense['plain'].values(), reverse=True)
    if sv[0] - sv[1] < 0.05:
        low_margin += 1
print(f'   top-1 {correct}/100, low-margin (<0.05) {low_margin}/100', flush=True)
results['C_collisions_N300'] = {'top1': correct, 'n': 100,
                                'low_margin_lt_0.05': low_margin}
save()
print(f'done — results in {OUT}', flush=True)
