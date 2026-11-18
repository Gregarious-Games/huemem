# HueMem recall — measured results

All numbers below were measured on a frozen 40-memory test set (40 PNGs,
SHA-256 verified against the frozen manifest, 2026-11-17) with 43 paraphrase
prompts (27 from the expanded set + 16 held-out), 9 two-turn cases and 10
exact prompts. The test corpus is one person's real conversation history, so
the numbers describe this corpus, not recall in general. Do not quote any
single figure without its set size beside it.

## Best measured system

A confidence-gated switch (Round 18), validated out of sample (Round 19):

- If the plain-language cosine's top-1 score is >= 0.40 **and** its
  margin over the runner-up is >= 0.05, answer with the plain-language
  ranker's top-1.
- Otherwise answer with the three-way fusion below (lexical IDF +
  word-list cosine + distinguishing-sentence cosine, RRF k=60).

**Paraphrase rank-1: 39/43 (91%). Two-turn: 9/9 first-turn, 9/9
follow-up. Exact: 10/10. Vocabulary-gap prompts: 6/6.**

The distinguishing sentence replaced the whole-.md cosine, which embedded
id/hash/dominant metadata noise. As a fourth ranker it added nothing
(32/43); as a replacement it added +2 paraphrase and +1 two-turn.

The plain-language sentence is a second one-sentence summary per memory,
in the plainest everyday words. Alone it scores 35/43 paraphrase and
6/6 on the vocabulary gap, but 8/10 exact and 6/9 two-turn; the gate
keeps its strengths and falls back to the fusion where it is unsure.
Validation: 200 random splits of the 43 prompts (22 to pick the gate
point, 21 to test it) give a test-half mean of 18.5/21 vs ~16.6/21 for
the fusion alone, and the (0.40, 0.05) point is chosen in 125/200
splits — the lift is structural, not tuning noise, though the full-set
39/43 is slightly optimistic (honest expectation ~38/43). Two-turn 9/9
and exact 10/10 were not split-validated (sets too small). The 4
remaining misses are all cases where the plain-language match is
confident but points at a neighbour. Caveat: the plain sentences were
human-authored knowing the test vocabulary; whether an automated writer
produces summaries that good is untested.

## Head-to-head vs plain text search

Same prompts, same memories. The text baseline is token overlap over the
.md sidecar files — no synonyms, no index, no embeddings (the standard
environment).

| | HueMem (best, H) | Plain text |
|---|---|---|
| Paraphrase rank-1 | 39/43 | 14/43 |
| Paraphrase Recall@3 | 42/43 | 19/43 |
| Paraphrase Recall@5 | 43/43 | 22/43 |
| Two-turn first-turn | 9/9 | 2/9 |
| Two-turn follow-up (with context) | 9/9 | 2/9 |
| Exact rank-1 | 10/10 | 9/10 |

Rank-1 is what a user experiences. Recall@3/@5 diagnose retrieval vs
ranking failure: 42/43 sit in the top 3 and all 43 in the top 5, so the
4 misses are ranking failures (ranked 2nd–4th), not retrieval failures.
Split: expanded 24/27 rank-1, held-out 15/16; Recall@3 expanded 26/27,
held-out 16/16. (The three-way fusion alone, W2, was 34/43 rank-1,
38/43 Recall@3, 7/9 two-turn, 10/10 exact.)

### Miss classification (best system, 4 misses)

Memory identifiers are anonymised here; the public repo carries no
personal memories, only the capability and these aggregate numbers.

- Near-neighbour collision (4): a lessons-decision memory vs a
  first-visit memory on the same family activity; a domain-screenshot
  memory vs a domain-price memory; a split-recovery memory vs the
  first-live-memory; a chosen product name vs a rejected product name.
  All four are cases where the plain-language match is confident but
  points at a neighbour; all four rank 2nd–4th.
- Vocabulary gap (0): closed. The plain-language signal answers all 6
  of the old gap prompts.
- Under-specified prompt (0 as primary class), test error (0), missing
  memory (0).

The previous system (W2, 34/43) had 9 misses: collision 4, vocabulary
gap 5. The gate fixed all 5 gap prompts and kept the rest, except it
flipped none of the old collisions — the 4 remaining misses are the
same collision family, now ranked 2nd–4th instead of 2nd–27th.

### Audit caveats (read before quoting the gap)

- The text baseline's rank-1 score is tied on 17/43 prompts, and ties break
  alphabetically, which is arbitrary. The expected memory loses the
  tie-break on 3/43, so text could fairly be 17/43. The gap remains at
  least 15, but 14/43 is noisier than it looks. Recall@3/@5 are sturdier.
- Text search *with* synonym expansion alone scores 25/43. Synonyms explain
  11 of the +18 rank-1 lead (34 vs 14, or +17 vs the tie-adjusted 17);
  the embedding and fusion layers add the rest.
- Two-turn "consistency" (follow-up top == first-turn top) measured 9/9
  with turn-1 context carried into the turn-2 query. That is an artifact
  of the context carry, not retrieval coherence. Quote follow-up-correct,
  not consistency. The follow-up-correct figure itself needs care: all 9
  turn-2 prompts are referential follow-ups to the SAME memory as turn 1
  ("what was its check result?", "who holds the key?"), so scoring them
  with no context at all (2/9) measures the wrong thing — a bare "its"
  cannot resolve alone, and a live agent never tries to. With just the
  turn-1 memory's one-sentence summary in the context window, follow-up
  is 7/9; the full word list and the first prompt add nothing beyond
  that sentence. 7/9 is the realistic live-agent number.
- Miss taxonomy on the 11 remaining misses: near-neighbour collision 6,
  vocabulary gap 4, under-specified prompt 1, test error 0, missing
  memory 0.

## What did not beat the in-house hybrid

Measured on the same frozen set, same prompts:

- Proper BM25 (k1=1.2, b=0.75): 32/43, two-turn 4/9.
- Snowflake arctic-embed-m-v2.0: 20/43, two-turn 1/9 — the leaderboard
  score does not transfer to short word-list documents.
- Qwen3-Embedding-0.6B: 29/43, two-turn 5/9.
- bge-reranker-v2-m3 on the best pool: 32/43, two-turn 6/9 — it rescued a
  bad pool (20/43 -> 32/43) but added nothing on a good one.
- HyDE with a small local generator (Qwen2.5-0.5B-Instruct): 27–28/43 —
  worse than baseline, and ~21s per query on CPU, unusable live. Note this
  tests HyDE with a weak generator, not HyDE with a strong one.
- HyDE at write time (Round 12): 3 hypothetical questions per memory from
  the same small generator, indexed as extra rankers — 29–32/43, two-turn
  4–5/9, exact 9/10, all worse than baseline. The questions use mostly
  new words (22% overlap with the memory's words) but the wrong words:
  plausible-sounding, imprecise, sometimes factually off. Only 1 of the 5
  vocabulary-gap misses moved, and 2 previously correct memories were
  lost. No format change to store the questions is justified.
- BGE-M3 dense (three-way RRF, .md cosine): 34/43 paraphrase, two-turn
  6/9, exact 10/10. It ties the best system on paraphrase but loses on
  two-turn (6/9 vs 7/9), so it does not beat the MPNet +
  distinguishing-sentence system. Note BGE-M3's dense embedding did beat
  MPNet's on the .md ranker (34 vs 32 paraphrase) — the distinguishing
  sentence is what keeps MPNet ahead. The sparse (SPLADE) variant could
  not run: this install's sentence-transformers rejects `return_sparse`
  for BGE-M3.
- Plain-language write-time sentence (Round 15): a second one-sentence
  summary per memory in the plainest everyday words, as a replacement
  for the distinguishing sentence (33/43) or a fourth RRF ranker
  (33/43) — both slightly worse than the best system, and the vocabulary
  gap still unmoved in the fused ranking. But a per-ranker diagnostic
  shows the plain-language cosine ranks the expected memory #1 on all 6
  vocabulary-gap prompts, while the other rankers rank it 6th–37th: the
  signal exists, and equal-weight RRF dilutes it away. The gap is a
  fusion problem, not a signal problem. Fusion variants (max fusion,
  weighted RRF, plain-only) are the next test.
- Fusion variants (Round 16): max fusion over the four rankers 33/43,
  two-turn 7/9, exact 10/10, gap 2/6; weighted RRF with the
  plain-language ranker at x2 or x3: 32–34/43, gap 1/6. Plain-language
  cosine alone: 35/43 paraphrase, R@3 40/43, gap 6/6 — but exact 8/10
  and two-turn 6/9, and its 8 misses are mostly near-neighbour
  collisions. Max fusion is too noisy: any ranker's #1 can win, and the
  tie-break goes to the wrong memory.
- Plain + distinguishing fusions (Round 17): plain+dist 30/43,
  plain+dist+lex 32/43, plain+dist+word 34/43 (R@3 39/43, two-turn 6/9,
  gap 2/6), max of the two 30/43 with R@3 42/43. None keeps both
  strengths. Max fusion's R@3 of 42/43 against R@1 of 30/43 says the
  signal is in the top 3 and the ranking is the problem. The remaining
  fusion idea is a score gate: trust plain-language cosine when it is
  confident, fall back to the three-way system otherwise.
- Automated plain summaries (Round 20): the same small generator
  (Qwen2.5-0.5B-Instruct) writing the plain-language sentences instead
  of a human — the gated system collapses to 34/43 paraphrase, two-turn
  7/9, exact 10/10, gap 1/6, back to the fusion's level. The model
  hallucinates and inverts facts (a sale becomes a purchase, 108,000
  miles becomes 10,800, wrong dates). Same failure mode as HyDE:
  plausible, imprecise, sometimes factually wrong. The 39/43 is a
  human-authored-summary number; a strong generator is untested and
  cannot be honestly measured offline.
- Distinguishing-sentence verification (Round 22): when the gate fires,
  override the plain answer with the fusion's if the plain top-1 ranks
  poorly on the distinguishing sentence — 35–36/43 at every threshold,
  gap 2–3/6, worse than the gated system on every metric. The
  distinguishing sentence agrees with the plain sentence on the wrong
  neighbours (dist-rank 0–2), so there is no disagreement signal to
  catch them, and the override fires where plain was right. The 4
  remaining misses are ranker-agreement collisions.
- Macro-research candidates (Round 23): CSLS as a ranker leg 33–34/43
  (worse than the fusion); CSLS as the gate signal at a swept point
  (k=5, 0.2, 0.05) 40/43, two-turn 9/9, exact 10/10, gap 6/6 — one
  prompt over the gated system, but split-half validation (Round 23b,
  200 splits) gives test-half 18.27/21 against the gated system's
  19.04/21: the lift is tuning noise, and it is worse than the gated
  system out of sample; whitening 35/43 (confirms the 2026 finding that
  it hurts retrieval-trained encoders); doc-conditioned margin at 0.2
  also 40/43 with the same full metrics, but split-half gives 18.95/21
  against 18.93/21 — identical, also tuning noise; distinguishing-only
  tie-break 39/43, exact 9/10 — no lift; discriminative Rocchio 35/43;
  MaxSim token matching 36/43 (plain tokens) and 31/43 (word-list
  tokens) as a standalone ranker, 33/43 as a fourth fusion leg.
- SPLADE learned sparse retrieval (Round 25,
  naver/splade-cocondenser-ensembledistil): as a fourth fusion leg
  33/43 (word docs) and 34/43 (plain docs, tying the fusion but losing
  on two-turn 6/9 and 8/9 and exact 9/10); standalone 24/43 (word
  docs) and 35/43 (plain docs, tying the plain ranker on paraphrase
  with 5/6 vocabulary-gap, but exact 6/10 and two-turn 4/9 and 7/9);
  replacing the lexical leg 33/43; as the gated system's fallback
  39/43 either way, but two-turn falls to 8/9. No variant beats the
  gated system.
- docT5query generation + relevance filtering (Round 26): the small
  generator writes 5 questions per memory from its plain sentence, a
  relevance filter keeps the top 3 by cosine to the plain sentence
  (120 kept, mean kept cosine 0.62), and the kept questions' mean
  embedding is a new doc vector. Standalone 32/43 (two-turn 4/9 and
  4/9, exact 6/10, though vocabulary-gap 6/6); as a fourth fusion leg
  32/43, worse than the fusion's 34/43; as the gated system's fallback
  37/43, worse than 39/43. The fourth weak-generator failure after
  HyDE at query time, HyDE at write time, and automated plain
  summaries — relevance filtering does not rescue the small generator.
- Contrastive fine-tuning on entity-swapped hard negatives (Round
  27): 65 distractors built by swapping one entity (proper noun,
  person, or number) in a memory's plain or source sentence for the
  same-category entity from a nearest-neighbour memory; 20/40 memories
  covered. Five-fold cross-validation over all 43 paraphrase prompts,
  fine-tuning the encoder with triplet loss (prompt, true memory,
  swapped distractor or rival) and re-tuning the gate per fold: the
  tuned encoder scores 37/43 against 34/43 for the base encoder put
  through the identical protocol — a real paired +3 lift, the first
  candidate that moved the needle in the right direction. But 37/43
  does not beat the gated system's 39/43, and exact recall dips to
  8/10 in one fold and two-turn to 8/9 in every fold, so it is not a
  system-level win. The remaining lever is data scale — a larger
  corpus would give the fine-tuning room to show a win — not another
  method. The macro-research queue is now fully exhausted.

## What is proven, and what is not

Proven on the measured sets: the writer and recovery contract (PNG alone
decodes the full word list after the sidecar is deleted; 40/40 verify PASS);
exact recall 10/10; paraphrase recall above plain text search on the same
prompts, with the audit caveats above.

Not proven: abstention. No single cosine threshold gives both zero false
refusals and full no-memory abstention (tau=0.20 keeps 34/34 correct and
abstains on 7/11 no-memory prompts; tau=0.35 gets 11/11 no-memory but
loses 7 correct). An ensemble of three cosine signals (top-1 cosine, mean
top-3, count above 0.20 — 60 combinations swept) adds nothing over the
single threshold: the best point is identical. The reason is in the
distributions — a wrong retrieval can score 0.64 on top-1 cosine, higher
than any correct answer's 0.61, and a no-memory prompt can have 7
memories above 0.20. Cosine to short word-list documents does not
separate "right answer" from "merely related". Until a reliable
abstention signal exists, the system can surface the right memory better
than text search, but it cannot reliably say "no memory answers this."
The operating point to use is tau=0.20. The cross-encoder reranker
score was tested as a gate (Round 13, bge-reranker-v2-m3 on the W2 top-8)
and fails worse than cosine: it scores ~0 on 12 of the 34 correct
retrievals, so any threshold that abstains on all 11 no-memory prompts
refuses at least 13 correct answers (tau=0.01 keeps 21/34). The reranker
responds to surface overlap, same as cosine — it does not judge "does
this answer the question" any better on this corpus. Reranking also hurts
ranking (32/43 vs 34/43). Conformal calibration (Round 14, split-half,
200 random splits of the 34 correct prompts) confirms the operating
point generalises: at a 5% false-refusal budget the calibrated threshold
is tau≈0.24, with ~6% actual false refusals on held-out prompts and
~72% no-memory abstention; at a 20% budget, tau≈0.36 and ~99%
no-memory abstention. Conformal picks the threshold honestly; it cannot
move the underlying overlap. On the gated system (H), the plain-language
cosine's top-1 score is a better abstention signal than the word cosine
(Round 21): no-memory prompts max out at 0.34 while correct answers go
down to 0.28, a narrow overlap band. At 0.25 it keeps 39/39 correct and
abstains on 6/11 no-memory; at 0.35 it abstains on 11/11 no-memory and
refuses only 2 correct (against 7 for the word cosine at full
abstention). Split-half calibration puts the honest threshold near 0.37,
with ~10% false refusals out of sample and ~98% no-memory abstention.
Gate A is still not passed in the strong sense, but the gated system's
abstention is strictly better than the fusion's. A logistic over the
whole score distribution (Round 24, RCS-style: top-1, margin, top-3
mean, entropy, lift, WIG, NQC, score gaps, k-th-NN distance, lexical
coverage, top-k coherence, plain + word + lexical legs, leave-one-out
cross-validation on 43 in-scope + 11 no-memory prompts) is the best
abstention result yet: LOO AUROC 0.998; at zero false refusals it
abstains on 10/11 no-memory prompts, and full no-memory abstention
costs only 1/43 false refusals — against 6/11 and 2/39 for the plain
top-1 threshold. One honest caveat: the no-memory test prompts are
systematically shorter than the paraphrase prompts (mean 8 tokens vs
14.5, ranges barely overlapping), and most features correlate with
length, so part of the separation is the test set's construction.
Removing query length from the logistic still gives AUROC 0.994, 8/11
at zero false refusals and 1/43 at full abstention, so the lift is not
only length — but 8/11 and 1/43 are the honest numbers to quote, not
10/11. The logistic answers "is there a memory at all?" — it carries
no signal about whether the top-1 retrieval is the right one (LOO
AUROC 0.212 on correct-vs-wrong, 39 vs 4, noisy but directionally
null), which matches Bacellar's warning that dense-only features carry
little mutual information about success. Gate A is still not passed in
the strong sense — full abstention still costs one false refusal — but
this is the closest yet, one prompt away. Shipped as an optional mode
in huemem_recall.py (`--abstain-logistic`, weights in
abstain_logistic_weights.json, no sklearn needed at runtime): the
module's features were verified against the test harness on a HUEM
re-encode of the 40-memory corpus (dense features match to 3e-08; the
two lexical features differ only because the public module uses its
generic synonym map while the harness used a corpus-specific one), and
end to end the module clears 43/43 in-scope and 10/11 no-memory
in-sample — the honest out-of-sample expectation stays the LOO 8/11
and 1/43. One stated limit: on a 2-memory corpus the logistic does
not abstain on the boat-cost prompt — the score-distribution features
are degenerate at that size, and the threshold was fitted on 40
memories.

## Accelerator library (2026-11-18)

`huemem_accel.py` — header-only PNG reader, derived index, vectorized
search, parity fault report. Full argument in `ACCELERATOR.md`.
Verified by `accel_test.py` (exit 0, 2026-11-18):

- Conversion CADE→HUEM: 40/40 word-identical. Header fast path:
  40/40 fields match; all five PNG filter types verified (12/12
  synthetic checks). Scan: 0.15 ms/PNG, 241x faster than a full load.
- Search equivalence: `AccelIndex.search` top-1 agrees with
  `RecallIndex.search` on all 53 prompts (0 diffs) — paraphrase 39/43,
  exact 10/10, two-turn t1 9/9 t2 8/9. The t2 8/9 is the public
  module's generic synonym map, not the accelerator: the original
  scores 8/9 on the same map (the private harness's corpus-specific
  map scores 9/9).
- Fault boundary: 9/9 cases pass (intact split, one child lost,
  parent lost, two children lost, parent + child lost, standalone
  intact / PNG lost / PNG corrupt / PNG + sidecar lost).
- An independent verification review found two real bugs (parent
  located by substring "-parent", so slugs containing "parent" broke
  verification; `fault_report` crashing on an unreadable parent) plus
  four more confirmed issues (manifest-corruption false
  "unrecoverable", split loaders reading the wrong PNG, a 1-memory
  gate crash, regrow trusting survivors without CRC checks). All fixed
  and covered by eight checks, 8/8 pass. One honest limit found in
  the fix: a missing highest-numbered child is undetectable when the
  parent is gone (no gap in the filename sequence); the report says
  so wherever it rebuilds without a manifest.

Benchmark (`accel_bench.py`, 2026-11-18; first run killed by a runtime
restart, re-run with Part C shrunk 1000→300 and per-part checkpointing):

- Per-query latency, full search path, 768-dim (primary run): N=100
  0.84 ms mean / 1.27 p95; N=1,000 7.74 / 8.99; N=5,000 49.83 / 64.23;
  N=10,000 110.16 / 132.02. A second run at 384 dims read 205 ms at
  N=10,000 — the box's throttle moves the large-N figure ~2x, so the
  honest claim is order of 100–200 ms at N=10,000 on this VM, plus
  ~50 ms query encode (constant).
- Build cost on this throttled box: MPNet 2.1 docs/s → ~4 h for a full
  N=10,000 build (3 legs); 200-memory build 265.8 s. Header scan 0.06
  ms/PNG, 323x faster than full load (small synthetic PNGs).
- Collisions at N=300 (synthetic near-neighbour stress test, real
  embeddings): top-1 74/100, low-margin (<0.05) 93/100 — the gate
  almost always falls back to fusion at this density. Compare with the
  40-memory baseline (4/43 near-misses). Collisions, not latency, are
  what degrades first.
- Index size: ~92 MB at N=10,000 (3 legs × N × 768 × 4 bytes).

## Latency (measured on a 1.5-CPU VM, MiniLM)

- One live query (encode + cosine over 40 precomputed embeddings): ~1.5s.
- Model cold load: ~5s, one-time, resident in live use.
- Encoding all 40 memories for the index: ~11.6s, one-time at index build.
- The 30+ minute test rounds are harness cost (60+ prompts x several
  models x re-encoding per prompt), not live query cost. Speed was
  declared secondary to recall; these numbers are reported, not claimed
  as an advantage.
