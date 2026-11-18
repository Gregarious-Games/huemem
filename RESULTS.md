# HueMem recall — measured results

All numbers below were measured on a frozen 40-memory test set (40 PNGs,
SHA-256 verified against the frozen manifest, 2026-11-17) with 43 paraphrase
prompts (27 from the expanded set + 16 held-out), 9 two-turn cases and 10
exact prompts. The test corpus is one person's real conversation history, so
the numbers describe this corpus, not recall in general. Do not quote any
single figure without its set size beside it.

## Best measured system

Three-way reciprocal-rank fusion (k=60) over:

1. lexical IDF overlap with synonym expansion (rare tokens, df<=2, count double)
2. dense cosine over the memory word list (all-mpnet-base-v2)
3. dense cosine over a one-sentence distinguishing description, written at
   write time, stating what makes the memory different from its neighbours

**Paraphrase rank-1: 34/43 (79%). Two-turn: 7/9 first-turn, 7/9 follow-up.
Exact: 10/10.**

The distinguishing sentence replaced the whole-.md cosine, which embedded
id/hash/dominant metadata noise. As a fourth ranker it added nothing
(32/43); as a replacement it added +2 paraphrase and +1 two-turn.

## Head-to-head vs plain text search

Same prompts, same memories. The text baseline is token overlap over the
.md sidecar files — no synonyms, no index, no embeddings (the standard
environment).

| | HueMem (best, W2) | Plain text |
|---|---|---|
| Paraphrase rank-1 | 34/43 | 14/43 |
| Paraphrase Recall@3 | 38/43 | 19/43 |
| Paraphrase Recall@5 | 38/43 | 22/43 |
| Two-turn first-turn | 7/9 | 2/9 |
| Two-turn follow-up (with context) | 7/9 | 2/9 |
| Exact rank-1 | 10/10 | 9/10 |

Rank-1 is what a user experiences. Recall@3/@5 diagnose retrieval vs
ranking failure: 38/43 sit in the top 3, so 5 misses are retrieval failures
(ranked 7th–27th) and 4 are ranking failures (ranked 2nd–3rd). Split:
expanded 21/27 rank-1, held-out 13/16; Recall@3 expanded 24/27,
held-out 14/16.

### Miss classification (best system, 9 misses)

Memory identifiers are anonymised here; the public repo carries no
personal memories, only the capability and these aggregate numbers.

- Near-neighbour collision (4): a lessons-decision memory vs a
  first-visit memory on the same family activity; a rejected product
  name vs the chosen product name; a domain-price memory vs a
  domain-screenshot memory (a flip: correct under the previous system);
  a vehicle-sale memory vs a vehicle-ad-deletion memory.
- Vocabulary gap (5): two messaging-blocked prompts ("reply to buyers"
  / "answering people who want to buy things" — the memory's words are
  PIN/password/reset/two-factor, almost no shared tokens); a heavily
  paraphrased repository-creation-failure prompt ("make a new home for
  itself on that site"); a colour-palette prompt ("colour system" vs RGB
  values); a rejected-name prompt ("photo app" vs the existing product).
- Under-specified prompt (0 as primary class), test error (0), missing
  memory (0).

The previous system (32/43) had 11 misses: collision 6, vocabulary gap 4,
under-specified 1. The distinguishing sentence fixed 3 collisions and
flipped 1. The 5 vocabulary-gap misses are unchanged across systems —
no ranking variant has moved them.

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
  of the context carry, not retrieval coherence: without context it
  collapses to 2/9 (HueMem) and 1/9 (text). Quote follow-up-correct,
  not consistency.
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
- BGE-M3 dense (three-way RRF, .md cosine): 34/43 paraphrase, two-turn
  6/9, exact 10/10. It ties the best system on paraphrase but loses on
  two-turn (6/9 vs 7/9), so it does not beat the MPNet +
  distinguishing-sentence system. Note BGE-M3's dense embedding did beat
  MPNet's on the .md ranker (34 vs 32 paraphrase) — the distinguishing
  sentence is what keeps MPNet ahead. The sparse (SPLADE) variant could
  not run: this install's sentence-transformers rejects `return_sparse`
  for BGE-M3.

## What is proven, and what is not

Proven on the measured sets: the writer and recovery contract (PNG alone
decodes the full word list after the sidecar is deleted; 40/40 verify PASS);
exact recall 10/10; paraphrase recall above plain text search on the same
prompts, with the audit caveats above.

Not proven: abstention. No single cosine threshold gives both zero false
refusals and full no-memory abstention (tau=0.20 on RRF tops keeps 32/32
correct and abstains on 7/11 no-memory prompts; tau=0.35 gets 11/11
no-memory but loses 6 correct). Until a reliable abstention signal exists,
the system can surface the right memory better than text search, but it
cannot reliably say "no memory answers this."

## Latency (measured on a 1.5-CPU VM, MiniLM)

- One live query (encode + cosine over 40 precomputed embeddings): ~1.5s.
- Model cold load: ~5s, one-time, resident in live use.
- Encoding all 40 memories for the index: ~11.6s, one-time at index build.
- The 30+ minute test rounds are harness cost (60+ prompts x several
  models x re-encoding per prompt), not live query cost. Speed was
  declared secondary to recall; these numbers are reported, not claimed
  as an advantage.
