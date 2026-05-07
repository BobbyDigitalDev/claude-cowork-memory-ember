# Memory Object Lifecycles

This document is the canonical reference for valid states, allowed transitions,
and script ownership for every object type in the ember-engine memory graph.

It exists so that status strings are never scattered as raw literals across
scripts without a single authoritative definition. When adding a new status or
transition, update this file first.

---

## beliefs

The most carefully governed object type. Beliefs carry epistemic weight and
influence retrieval ranking, verification, and session context.

### Valid statuses

| Status | Meaning | Retrieval weight |
|---|---|---|
| `proposed` | Extracted by Qwen, not yet verified against source | 0.75× (damped) |
| `supported` | R1 confirms with caveats, or weak challenge present | 0.85× |
| `verified` | R1 confirms with direct evidence, challenge absent/trivial | 1.0× |
| `disputed` | Challenge outweighs support, or belief misrepresents source | 0.35× |
| `deprecated` | Superseded by a newer belief; kept for provenance | 0.10× |
| `archived` | Manually retired; excluded from active retrieval | 0.05× |

### Allowed transitions

```
proposed  →  supported       (verify_beliefs.py: R1 confirms with caveats)
proposed  →  verified        (verify_beliefs.py: R1 confirms clearly)
proposed  →  disputed        (verify_beliefs.py: R1 finds contradicting evidence)
supported →  verified        (verify_beliefs.py: subsequent pass, stronger evidence)
supported →  disputed        (verify_beliefs.py: later challenge found)
verified  →  disputed        (verify_beliefs.py: new contradicting evidence)
any       →  disputed        (verify_beliefs.py: cross-belief contradiction check)
any       →  deprecated      (memory_curator.py: superseded by newer belief)
any       →  archived        (manual: via review_scout.py or direct DB update)
```

### Script ownership

| Transition | Owned by |
|---|---|
| `proposed` creation | `ingest.py`, `process_conversation.py`, `process_research.py` |
| `proposed → supported/verified/disputed` | `verify_beliefs.py` |
| `any → deprecated` | `memory_curator.py` |
| `any → archived` | Manual / future curator tool |
| Cross-belief contradiction → `disputed` | `verify_beliefs.py --check-contradictions` |

### Additional fields

- `confidence_score` (float 0.0–1.0): calibrated by `verify_beliefs.py`
- `confidence_calibrated` (0/1): 0 = raw extraction score, 1 = verified against evidence
- `fidelity_score` (float 0.0–1.0): how faithfully the stored position represents the verbatim anchor; set by `verify_beliefs.py`; low (<0.6) = extraction distortion
- `verbatim_anchor`: source quote the belief was extracted from
- `challenge_history`: JSON array of challenge events (date, text, source)
- `last_verified_at`: timestamp of most recent `verify_beliefs.py` pass

### Retrieval behavior

Semantic retrieval applies **epistemic damping**: the cosine similarity score
is multiplied by the status multiplier before comparison against threshold.
Disputed and deprecated beliefs are pushed below the retrieval threshold even
when semantically close to the query. This prevents stale or wrong beliefs from
dominating context.

---

## epiphanies

Similar lifecycle to beliefs but less formally governed. Epiphanies are
non-obvious insights rather than propositions, so verification is less strict.

### Valid statuses

| Status | Meaning |
|---|---|
| `proposed` | Extracted, not reviewed |
| `verified` | Confirmed as genuine insight |
| `archived` | No longer relevant |

Epiphanies do not currently go through `verify_beliefs.py`. Manual review via
`inspect_memory.py` is the intended path.

---

## questions

Open questions drive research queries in `research_scout.py` and shape session
context. They accumulate until explicitly closed.

### Valid statuses

| Status | Meaning |
|---|---|
| `open` | Unanswered; actively drives research |
| `answered` | A satisfying answer was reached and recorded in `current_best_thinking` |
| `closed` | No longer relevant; not driving research |

### Script ownership

| Transition | Owned by |
|---|---|
| `open` creation | `ingest.py`, `process_conversation.py`, `process_research.py` |
| `open → answered` | Manual / session review |
| `open → closed` | Manual |

Questions older than 30 days with no activity are surfaced as "stale" by
`memory_health.py` to prompt review.

---

## memory_chunks

Chunks are the semantic retrieval layer. Every conversation excerpt and research
passage passes through chunking before being embedded.

### Valid statuses (embedding_status)

| Status | Meaning |
|---|---|
| `pending` | Chunk written to DB, not yet embedded |
| `embedded` | nomic-embed-text vector stored; chunk is retrievable via cosine search |
| `failed` | Embedding attempt failed (Ollama unreachable) |

### Script ownership

| Transition | Owned by |
|---|---|
| `pending` creation | `ingest.py`, `embed_memories.py` |
| `pending → embedded` | `embed_memories.py` |
| `pending → failed` | `embed_memories.py` (on Ollama timeout/error) |

### Negation re-embedding

When a belief is marked `disputed` or `deprecated`, `verify_beliefs.py` calls
`_negate_and_reembed()` on all chunks linked to that belief. The chunk content
is prefixed with `[REFUTED] — {username} no longer holds this position:` and
re-embedded. This shifts the chunk vector away from the positive-claim region
so it stops surfacing on that topic in future retrievals.

---

## processing_jobs

Tracks every background extraction, embedding, and verification run.

### Valid statuses

| Status | Meaning |
|---|---|
| `pending` | Job queued, not yet started |
| `started` | Agent picked up the job; row exists, writes in progress |
| `completed` | All expected writes succeeded |
| `completed_with_warnings` | Job finished but one or more row/provenance writes failed; `error_log` has details |
| `failed` | Unrecoverable error; check `error_log` |

### Script ownership

| Transition | Owned by |
|---|---|
| `pending` creation | `ingest.py`, `auto_ingest.py`, `research_scout.py` |
| `pending → started` | `ingest_agent.py`, `process_conversation.py` |
| `started → completed` | Same script (all writes succeeded) |
| `started → completed_with_warnings` | Same script (partial write failures; `error_log` populated) |
| `started → failed` | Same script (on unrecoverable exception) |
| `any → failed` | Same script (file-level error before writes begin) |

Pending jobs are surfaced in `doctor.py` and `memory_health.py`.
Failed and `completed_with_warnings` jobs appear in `memory_health.py` with error excerpts.

---

## scout_results

Research Scout results awaiting Curator review before any belief impact occurs.

### Valid statuses

| Status | Meaning |
|---|---|
| `pending` | Fetched, not yet reviewed |
| `interesting` | Manually flagged for follow-up |
| `reviewed` | Reviewed but not promoted |
| `dismissed` | Reviewed and discarded |
| `ingested` | Promoted to memory (belief, concept, or research task) |

### Script ownership

| Transition | Owned by |
|---|---|
| `pending` creation | `research_scout.py` |
| `pending → interesting/dismissed` | `review_scout.py --mark ID --status` |
| `interesting → ingested` | `review_scout.py --ingest ID` |
| `pending → reviewed` | `review_scout.py --all` |

### Scoring fields

- `relevance_score` (0.0–1.0): cosine similarity of paper abstract against `memory_chunks`
- `challenge_score` (0.0–1.0): cosine divergence from belief-space centroid; high = challenges existing beliefs; set by `research_scout.py` as of 2026-05-05

---

## goals

Goals are tracked separately from the main memory graph and are not versioned.

### Valid statuses

| Status | Meaning |
|---|---|
| `active` | In progress |
| `completed` | Done |
| `deprecated` | No longer relevant |

Goals are surfaced in session context by both semantic (if query-relevant) and
temporal (if recently created) retrieval strategies.

---

## Session metadata types: moods, gratitude, boundaries

`moods`, `gratitude`, and `boundaries` are written by `process_conversation.py` but
are **intentionally excluded from the provenance system**. They are session-scoped
metadata, not extractable knowledge objects, for the following reasons:

- **No lifecycle**: they have no `status` field, no verification path, and are never
  promoted, deprecated, or archived. They record what happened in a session; they
  do not make claims that need to be checked over time.
- **No retrieval weight**: the retrieval pipeline (`retrieve.py`) does not score or
  surface moods, gratitude entries, or boundaries via semantic or structural strategies.
  They are consulted only by `memory_health.py` for session-quality reporting.
- **No epistemic risk**: unlike beliefs or epiphanies, these types do not assert facts
  about the world. Provenance tracking exists to support trust inspection and future
  re-verification; those concerns do not apply to session tone or acknowledged moments.

`memory_provenance` rows are therefore not written for these three types, and no
`memory_origin` column is defined on their tables. This is a deliberate design
decision, not an oversight.

If a future use case requires tracking where a specific boundary decision came from
(e.g., attributing it to a specific conversation for replay), the design should be
revisited and a migration + provenance write added at that time.

---

## Shared conventions

**`memory_origin`** — set on beliefs, epiphanies, concepts, entities, patterns, and
questions to distinguish how the object entered the system:

| Value | Meaning |
|---|---|
| `conversation` | Extracted from a Bobby-Claude session transcript |
| `research` | Extracted from an external source (YouTube, PubMed, arXiv, etc.) |
| `manual` | Entered directly by Bobby |
| `agent_generated` | Created by a background agent without a direct source |
| `imported` | Bulk imported from an external system |

**`is_active`** — binary flag. `0` means the object is logically deleted (never
physically removed). Retrieval, verification, and reports filter on `is_active=1`.

**`importance_score`** — float 0.0–1.0, used as a tiebreaker in retrieval and as
a severity proxy in tension records.

---

*Last updated: 2026-05-05 — ember-engine v1.x*
