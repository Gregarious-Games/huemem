"""HueMem recall — derived index + retrieval over HUEM PNG memories.

The PNG remains the authoritative record (identity, category, hashes, full
ordered word list). This module builds a *derived, regenerable* index from the
PNGs and their .md sidecars, and retrieves with the best measured system —
a confidence-gated switch (Round 18, split-half validated in Round 19):

  - If the plain-language cosine's top-1 score is >= 0.40 AND its margin
    over the runner-up is >= 0.05, answer with the plain-language ranker.
  - Otherwise answer with three-way RRF (k=60) over
    1. lexical IDF overlap with synonym expansion (rare tokens df<=2 count double)
    2. dense cosine over the memory word list (default all-mpnet-base-v2)
    3. dense cosine over the distinguishing sentence

The distinguishing sentence is one sentence per memory, written at write time,
stating what makes that memory different from its neighbours. The plain-language
sentence is a second one-sentence summary per memory, in the plainest everyday
words. Both live in the memory's .md sidecar as `distinguishing:` and `plain:`
lines; when absent, the `source:` sentence is used instead, and the gate is
disabled (no memory carries an explicit plain sentence). Measured on the
frozen 40-memory test set, the gated system scored paraphrase rank-1 39/43
(two-turn 9/9, exact 10/10, vocabulary-gap prompts 6/6) against 34/43 for
the three-way fusion alone and 35/43 for the plain-language ranker alone.
Split-half validation (200 splits, 22 to pick the gate point, 21 to test
it) gives a test-half mean of 18.5/21 vs ~16.6/21 for the fusion alone, so
the lift is structural, not tuning noise — though the full-set 39/43 is
slightly optimistic (honest expectation ~38/43).

Caveat: the measured plain sentences were human-authored. A small local
generator (Qwen2.5-0.5B-Instruct, Round 20) writing them instead collapses
the gated system to 34/43 — it hallucinates and inverts facts. Whether an
automated writer can produce summaries as good as a human's is untested;
do not quote the 39/43 as an automated system's number.

Abstention: when plain-language sentences exist, the abstention signal is
the plain-language cosine's top-1 score (Round 21). Below 0.25 the system
abstains: on the test set that kept 39/39 correct answers and abstained on
6/11 no-memory prompts; 0.35 abstains on 11/11 no-memory but refuses 2
correct. Split-half calibration puts the honest threshold near 0.37, with
~10% false refusals out of sample and ~98% no-memory abstention. Without
plain sentences, abstention falls back to the word cosine below 0.20
(kept 34/34 correct, abstained on 7/11 no-memory on the fusion alone).
A logistic over the whole score distribution (Round 24, RCS-style:
top-1, margin, top-3 mean, entropy, lift, WIG, NQC, score gaps, k-th-NN
distance, lexical coverage, top-k coherence across the plain, word and
lexical legs; fitted on the 43 in-scope + 11 no-memory test prompts,
query length deliberately excluded) beats the single threshold: with
--abstain-logistic it abstains on 8/11 no-memory prompts at zero false
refusals (leave-one-out AUROC 0.994), and full no-memory abstention
costs only 1/43 false refusals. The weights live in
abstain_logistic_weights.json next to this module and are the honest
defaults — fitted on the 40-memory test set, refit on your own corpus
when you have labelled no-memory prompts. One limit stands: the
logistic answers "is there a memory at all?", not "is this the right
one?" (correctness AUROC 0.212 — null). There is still no single
mechanism that gives both zero false refusals and full abstention —
quote the operating point with the numbers, not as a solved problem.

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
from huemem import read_png, decode_payload, bare_slug, load_memory_words

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

def load_logistic_weights():
    """Round 24 abstention-logistic weights (abstain_logistic_weights.json).

    Numbers only: feature names, scaler mean/scale, coefficients,
    intercept, and the LOO-calibrated threshold (0 false refusals on the
    test set). Returns None when the file is absent — the caller then
    falls back to the plain-cosine threshold.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'abstain_logistic_weights.json')
    if not os.path.exists(path):
        return None
    return json.load(open(path))

def parse_md(path):
    """Read a memory .md sidecar: source + optional distinguishing/plain lines."""
    src = dist = plain = ''
    for line in open(path, encoding='utf-8', errors='replace'):
        if line.startswith('- source: '):
            src = line[len('- source: '):].strip()
        elif line.startswith('- distinguishing: '):
            dist = line[len('- distinguishing: '):].strip()
        elif line.startswith('- plain: '):
            plain = line[len('- plain: '):].strip()
    return src, dist, plain

def load_memories(mem_dir):
    """Decode every HUEM PNG in mem_dir. Returns {slug: {words, source, dist, plain}}.

    Keys are bare slugs (the trailing -<hash8> dir token is stripped),
    so search results match the `- slug:` field in the .md sidecars.
    """
    out = {}
    for d in sorted(os.listdir(mem_dir)):
        dpath = os.path.join(mem_dir, d)
        if not os.path.isdir(dpath):
            continue
        pngs = [f for f in os.listdir(dpath) if f.endswith('.png')]
        mds = [f for f in os.listdir(dpath) if f.endswith('.md')]
        if not pngs:
            continue
        slug = bare_slug(d)
        if slug in out:
            print(f"warning: duplicate memory dir for slug {slug} ({d}) — skipped",
                  file=sys.stderr)
            continue
        tagged, header, _is_split = load_memory_words(dpath)
        words = [w for w, _ in tagged]
        src, dist, plain = parse_md(os.path.join(dpath, mds[0])) if mds else ('', '', '')
        out[slug] = {'words': words, 'source': src, 'dist': dist or src,
                     'plain': plain, 'has_plain': bool(plain)}
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
            self.has_plain = any(memories[s]['has_plain'] for s in self.slugs)
            self.emb_plain = self._emb([memories[s]['plain'] or memories[s]['source']
                                        for s in self.slugs]) if self.has_plain else None

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

    def _plain_doc_sim(self):
        """Pairwise plain-language cosine between memories (for coherence)."""
        if getattr(self, '_plain_sim', None) is None:
            if self.emb_plain is None:
                return None
            E = self.np.stack([self.emb_plain[s] for s in self.slugs])
            self._plain_sim = E @ E.T
        return self._plain_sim

    def abstain_features(self, prompt, word_cos, plain_cos):
        """Score-distribution features for the Round 24 abstention logistic.

        Same definitions as the test harness (abstention_logistic_test.py):
        per leg (plain, word) — top-1, margin, top-3 mean, entropy of the
        top-10 softmax, lift, WIG, NQC, s1-s5, s1-s10, 1-s3, 1-s5, and
        top-3 doc-doc coherence measured on the *plain* embeddings for
        both legs — plus lexical coverage and mean query IDF. Score lists
        are padded with their minimum to length 10 so small corpora do
        not crash; the weights were fitted on a 40-memory corpus.
        """
        np = self.np
        feats = {}
        sim = self._plain_doc_sim()
        for leg, scores in (('plain', plain_cos), ('word', word_cos)):
            if scores is None:
                for k in ('s1', 'margin', 'top3mean', 'entropy', 'lift', 'wig',
                          'nqc', 's1s5', 's1s10', 'd3', 'd5', 'coh3'):
                    feats[f'{leg}_{k}'] = 0.0
                continue
            v = np.array(sorted(scores.values(), reverse=True), dtype=float)
            if len(v) < 10:
                v = np.concatenate([v, np.full(10 - len(v), v[-1])])
            p = np.exp(v - v.max()); p = p / p.sum()
            feats[f'{leg}_s1'] = float(v[0])
            feats[f'{leg}_margin'] = float(v[0] - v[1])
            feats[f'{leg}_top3mean'] = float(v[:3].mean())
            feats[f'{leg}_entropy'] = float(-(p[:10] * np.log(p[:10] + 1e-12)).sum())
            feats[f'{leg}_lift'] = float(v[0] / max(v.mean(), 1e-6))
            feats[f'{leg}_wig'] = float(v[:5].mean() - v.mean())
            feats[f'{leg}_nqc'] = float(v[:5].std() / max(v.mean(), 1e-6))
            feats[f'{leg}_s1s5'] = float(v[0] - v[4])
            feats[f'{leg}_s1s10'] = float(v[0] - v[9])
            feats[f'{leg}_d3'] = float(1 - v[2])
            feats[f'{leg}_d5'] = float(1 - v[4])
            if sim is not None:
                k = min(3, len(self.slugs))
                topk = [self.slugs.index(s) for s, _ in
                        sorted(scores.items(), key=lambda kv: -kv[1])[:k]]
                if k >= 2:
                    feats[f'{leg}_coh3'] = float(
                        sim[np.ix_(topk, topk)][np.triu_indices(k, 1)].mean())
                else:
                    feats[f'{leg}_coh3'] = 0.0
            else:
                feats[f'{leg}_coh3'] = 0.0
        qt = expand(set(tokens(prompt)))
        idfs = [self._idf(t) for t in qt]
        covered = [t for t in qt if any(t in self.mem_tokens[s] for s in self.slugs)]
        feats['lex_cov'] = (sum(self._idf(t) for t in covered) /
                            max(sum(idfs), 1e-6)) if idfs else 0.0
        feats['lex_meanidf'] = float(np.mean(idfs)) if idfs else 0.0
        return feats

    def logistic_prob(self, prompt, word_cos, plain_cos, weights=None):
        """P(in-scope) from the Round 24 logistic; None when unavailable."""
        weights = weights if weights is not None else getattr(self, '_logistic_weights', None)
        if weights is None:
            weights = load_logistic_weights()
            self._logistic_weights = weights
        if weights is None or self.encoder is None:
            return None
        feats = self.abstain_features(prompt, word_cos, plain_cos)
        try:
            x = self.np.array([feats[name] for name in weights['features']], dtype=float)
        except KeyError:
            return None
        z = (x - self.np.array(weights['scaler_mean'])) / self.np.array(weights['scaler_scale'])
        logit = float(self.np.dot(self.np.array(weights['coef']), z) + weights['intercept'])
        logit = max(min(logit, 30.0), -30.0)
        return 1.0 / (1.0 + math.exp(-logit)), weights['threshold']

    def _rank(self, scores):
        return {s: i for i, (s, _) in enumerate(
            sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])))}

    def search(self, prompt, top=5, abstain=False, abstain_tau=0.20,
               gate=True, gate_tau_s=0.40, gate_tau_m=0.05,
               abstain_plain_tau=0.25, abstain_logistic=False):
        """Return [(slug, score, word_cosine)] best-first, or [] on abstention.

        With the gate on (default) and plain sentences present: if the
        plain-language cosine's top-1 score >= gate_tau_s and its margin
        over #2 >= gate_tau_m, the ranking is the plain-language ranking;
        otherwise it is the three-way RRF. The returned score is the
        plain cosine in the gated case, the RRF score otherwise.

        Abstention: by default the plain-language cosine's top-1 score is
        the signal (Round 21): below abstain_plain_tau (default 0.25) the
        system abstains. Measured on the test set that kept 39/39 correct
        answers and abstained on 6/11 no-memory prompts; 0.35 abstains on
        11/11 no-memory but refuses 2 correct. With abstain_logistic=True
        the Round 24 logistic over the whole score distribution decides
        instead (8/11 no-memory at zero false refusals, 1/43 false
        refusals at full abstention — leave-one-out, query length
        excluded). The logistic needs plain sentences; without them, or
        when its weights file is absent, it falls back to the cosine
        threshold. Without plain sentences at all, abstention falls back
        to the word cosine < abstain_tau.
        """
        ranks = [self._rank(self.lex_scores(prompt))]
        word_cos = plain_cos = None
        plain_ranking = None
        if self.encoder is not None:
            qe = self.encoder.encode([prompt], normalize_embeddings=True)[0]
            word_cos = {s: float(self.np.dot(qe, self.emb_word[s])) for s in self.slugs}
            dist_cos = {s: float(self.np.dot(qe, self.emb_dist[s])) for s in self.slugs}
            ranks.append(self._rank(word_cos))
            ranks.append(self._rank(dist_cos))
            if self.emb_plain is not None:
                plain_cos = {s: float(self.np.dot(qe, self.emb_plain[s])) for s in self.slugs}
                if gate:
                    sc = sorted(plain_cos.values(), reverse=True)
                    margin = sc[0] - sc[1] if len(sc) > 1 else float("inf")
                    if sc[0] >= gate_tau_s and margin >= gate_tau_m:
                        plain_ranking = sorted(plain_cos.items(), key=lambda kv: (-kv[1], kv[0]))
        def should_abstain(top_slug):
            # The abstention signal is the best plain-language match to the
            # query overall (max over memories), not the plain score of the
            # fusion's top-1.
            if abstain_logistic and plain_cos is not None:
                res = self.logistic_prob(prompt, word_cos, plain_cos)
                if res is not None:
                    prob, threshold = res
                    return prob < threshold
            if plain_cos is not None and max(plain_cos.values()) < abstain_plain_tau:
                return True
            if plain_cos is None and word_cos is not None and word_cos[top_slug] < abstain_tau:
                return True
            return False
        if plain_ranking is not None:
            ranked = plain_ranking[:top]
            if abstain and should_abstain(ranked[0][0]):
                return []
            return [(s, score, (word_cos or {}).get(s)) for s, score in ranked]
        fused = {s: sum(1 / (60 + r[s] + 1) for r in ranks) for s in self.slugs}
        ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
        if abstain and should_abstain(ranked[0][0]):
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
    sp.add_argument('--no-gate', action='store_true',
                    help='disable the plain-language confidence gate (three-way RRF only)')
    sp.add_argument('--gate-tau-s', type=float, default=0.40)
    sp.add_argument('--gate-tau-m', type=float, default=0.05)
    sp.add_argument('--abstain-plain-tau', type=float, default=0.25,
                    help='plain-language cosine threshold for abstention when plain sentences exist')
    sp.add_argument('--abstain-logistic', action='store_true',
                    help='use the Round 24 score-distribution logistic for abstention '
                         '(8/11 no-memory at zero false refusals; needs plain sentences '
                         'and abstain_logistic_weights.json, else falls back to the cosine threshold)')
    ip = sub.add_parser('index')
    ip.add_argument('mem_dir')
    args = ap.parse_args()
    memories = load_memories(args.mem_dir)
    if not memories:
        print(f'no HUEM memories found in {args.mem_dir}')
        return 1
    if args.cmd == 'index':
        print(json.dumps({s: {'words': m['words'], 'source': m['source'],
                              'distinguishing': m['dist'], 'plain': m['plain']}
                          for s, m in memories.items()}, indent=2))
        return 0
    idx = RecallIndex(memories, dense_model=None if args.lexical_only else args.dense_model)
    hits = idx.search(args.query, top=args.top, abstain=args.abstain, abstain_tau=args.tau,
                      gate=not args.no_gate, gate_tau_s=args.gate_tau_s, gate_tau_m=args.gate_tau_m,
                      abstain_plain_tau=args.abstain_plain_tau,
                      abstain_logistic=args.abstain_logistic)
    if not hits:
        print('abstained: no memory clears the threshold')
        return 0
    for slug, score, cos in hits:
        print(f'{slug}  rrf={score:.4f}' + (f'  word_cos={cos:.2f}' if cos is not None else ''))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
