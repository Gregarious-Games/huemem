"""HueMem recall — derived index + retrieval over HUEM PNG memories.

The PNG remains the authoritative record (identity, category, hashes, full
ordered word list). This module builds a *derived, regenerable* index from the
PNGs and their .md sidecars, and retrieves with the best measured system:

  three-way RRF (k=60) over
    1. lexical IDF overlap with synonym expansion (rare tokens df<=2 count double)
    2. dense cosine over the memory word list (default all-mpnet-base-v2)
    3. dense cosine over the distinguishing sentence

The distinguishing sentence is one sentence per memory, written at write time,
stating what makes that memory different from its neighbours. It lives in the
memory's .md sidecar as a `distinguishing:` line; when absent, the `source:`
sentence is used instead. Measured on the frozen 40-memory test set, replacing
the whole-.md cosine with the distinguishing-sentence cosine lifted paraphrase
rank-1 from 32/43 to 34/43 (two-turn 6/9 -> 7/9, exact 10/10).

Abstention: the top result's dense word cosine below 0.20 abstains. On the
test set that operating point kept 32/32 correct answers and abstained on
7/11 no-memory prompts; a stricter 0.35 abstains on 11/11 no-memory but loses
6 correct answers. There is no single threshold that gets both — quote the
operating point with the numbers, not as a solved problem.

No watcher, no autonomous writes. The index is rebuilt on demand from the
PNGs; deleting the index loses nothing.

Usage:
  python3 huemem_recall.py search <memories-dir> "<query>" [--top N] [--abstain]
  python3 huemem_recall.py index <memories-dir>   # print the derived index as JSON

Requires: sentence-transformers (for the dense model). The lexical layer works
without it (pass --lexical-only).
"""

import argparse, json, math, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from huemem import read_png, decode_payload

# Generic English synonym groups (small, extensible). The measured test set
# used a corpus-specific map; on another corpus these groups are a starting
# point, and the measured numbers do not transfer automatically.
SYNONYM_GROUPS = [
    ['big', 'large', 'huge'],
    ['small', 'little', 'tiny'],
    ['buy', 'purchase', 'bought'],
    ['sell', 'sold', 'sale'],
    ['car', 'vehicle', 'automobile'],
    ['house', 'home', 'residence'],
    ['child', 'kid', 'kiddo'],
    ['fast', 'quick', 'rapid'],
    ['slow', 'gradual'],
    ['begin', 'start', 'started'],
    ['end', 'finish', 'finished'],
    ['ask', 'question', 'inquire'],
    ['answer', 'reply', 'respond'],
    ['price', 'cost', 'amount'],
    ['photo', 'picture', 'image'],
    ['file', 'document'],
    ['code', 'program', 'software'],
    ['public', 'open'],
    ['private', 'personal'],
    ['create', 'make', 'build'],
    ['delete', 'remove', 'erase'],
    ['save', 'store', 'keep'],
    ['find', 'locate', 'search'],
    ['choose', 'chose', 'select', 'pick'],
    ['name', 'called', 'titled'],
]

def build_synonym_map(groups=SYNONYM_GROUPS):
    m = {}
    for g in groups:
        s = set(g)
        for t in g:
            m.setdefault(t, set()).update(s)
    return m

SYN = build_synonym_map()

def tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower())

def expand(tok_set):
    out = set(tok_set)
    for t in tok_set:
        out |= SYN.get(t, set())
    return out

def parse_md(path):
    """Read a memory .md sidecar: source sentence + optional distinguishing line."""
    src = dist = ''
    for line in open(path, encoding='utf-8', errors='replace'):
        if line.startswith('- source: '):
            src = line[len('- source: '):].strip()
        elif line.startswith('- distinguishing: '):
            dist = line[len('- distinguishing: '):].strip()
    return src, dist

def load_memories(mem_dir):
    """Decode every HUEM PNG in mem_dir. Returns {slug: {words, source, dist}}."""
    out = {}
    for d in sorted(os.listdir(mem_dir)):
        dpath = os.path.join(mem_dir, d)
        if not os.path.isdir(dpath):
            continue
        pngs = [f for f in os.listdir(dpath) if f.endswith('.png')]
        mds = [f for f in os.listdir(dpath) if f.endswith('.md')]
        if not pngs:
            continue
        header, payload, _ = read_png(os.path.join(dpath, pngs[0]))
        words = [w for w, _ in decode_payload(header, payload)]
        src, dist = parse_md(os.path.join(dpath, mds[0])) if mds else ('', '')
        out[d] = {'words': words, 'source': src, 'dist': dist or src}
    return out

class RecallIndex:
    def __init__(self, memories, dense_model='all-mpnet-base-v2'):
        self.memories = memories
        self.slugs = sorted(memories)
        self.N = len(self.slugs)
        self.mem_tokens, self.df = {}, {}
        for slug, m in memories.items():
            mt = expand(set(tokens(' '.join(m['words']))) | set(tokens(slug)))
            self.mem_tokens[slug] = mt
            for t in mt:
                self.df[t] = self.df.get(t, 0) + 1
        self.encoder = None
        if dense_model:
            from sentence_transformers import SentenceTransformer
            self.encoder = SentenceTransformer(dense_model)
            import numpy as np
            self.np = np
            self.emb_word = self._emb([' '.join(memories[s]['words']) for s in self.slugs])
            self.emb_dist = self._emb([memories[s]['dist'] for s in self.slugs])

    def _emb(self, texts):
        e = self.encoder.encode(texts, normalize_embeddings=True)
        return {s: e[i] for i, s in enumerate(self.slugs)}

    def _idf(self, t):
        return math.log(self.N / max(1, self.df.get(t, 1))) + 1.0

    def lex_scores(self, prompt):
        qt = expand(set(tokens(prompt)))
        out = {}
        for slug, mt in self.mem_tokens.items():
            s = sum(self._idf(t) * (2.0 if self.df.get(t, 99) <= 2 else 1.0)
                    for t in qt & mt)
            s += 0.5 * len(qt & set(tokens(slug)))
            out[slug] = s
        return out

    def _rank(self, scores):
        return {s: i for i, (s, _) in enumerate(
            sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])))}

    def search(self, prompt, top=5, abstain=False, abstain_tau=0.20):
        """Return [(slug, rrf_score, word_cosine)] best-first, or [] on abstention."""
        ranks = [self._rank(self.lex_scores(prompt))]
        word_cos = None
        if self.encoder is not None:
            qe = self.encoder.encode([prompt], normalize_embeddings=True)[0]
            word_cos = {s: float(self.np.dot(qe, self.emb_word[s])) for s in self.slugs}
            dist_cos = {s: float(self.np.dot(qe, self.emb_dist[s])) for s in self.slugs}
            ranks.append(self._rank(word_cos))
            ranks.append(self._rank(dist_cos))
        fused = {s: sum(1 / (60 + r[s] + 1) for r in ranks) for s in self.slugs}
        ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
        if abstain and word_cos is not None and word_cos[ranked[0][0]] < abstain_tau:
            return []
        return [(s, score, (word_cos or {}).get(s)) for s, score in ranked[:top]]

def main():
    ap = argparse.ArgumentParser(description='HueMem recall over HUEM PNG memories.')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sp = sub.add_parser('search')
    sp.add_argument('mem_dir')
    sp.add_argument('query')
    sp.add_argument('--top', type=int, default=5)
    sp.add_argument('--abstain', action='store_true')
    sp.add_argument('--tau', type=float, default=0.20)
    sp.add_argument('--lexical-only', action='store_true')
    sp.add_argument('--dense-model', default='all-mpnet-base-v2')
    ip = sub.add_parser('index')
    ip.add_argument('mem_dir')
    args = ap.parse_args()
    memories = load_memories(args.mem_dir)
    if not memories:
        print(f'no HUEM memories found in {args.mem_dir}')
        return 1
    if args.cmd == 'index':
        print(json.dumps({s: {'words': m['words'], 'source': m['source'],
                              'distinguishing': m['dist']}
                          for s, m in memories.items()}, indent=2))
        return 0
    idx = RecallIndex(memories, dense_model=None if args.lexical_only else args.dense_model)
    hits = idx.search(args.query, top=args.top, abstain=args.abstain, abstain_tau=args.tau)
    if not hits:
        print('abstained: no memory clears the threshold')
        return 0
    for slug, score, cos in hits:
        print(f'{slug}  rrf={score:.4f}' + (f'  word_cos={cos:.2f}' if cos is not None else ''))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
