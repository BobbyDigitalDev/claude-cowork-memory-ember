#!/usr/bin/env python3
"""
research_scout.py
-----------------
Research Scout Agent for the Claude Memory System.

Runs daily to surface relevant external content from academic and science
journalism sources. Writes candidates to scout_results for Curator review.
Never writes directly to beliefs or concepts.

USAGE
-----
Normal daily run (called by launchd or manually):
    python3 ~/claude_memory/scripts/research_scout.py

Dry run (query and score but do not write to database):
    python3 ~/claude_memory/scripts/research_scout.py --dry-run

Refresh the ring-2 topic cache regardless of age:
    python3 ~/claude_memory/scripts/research_scout.py --refresh-ring2

Show what seeds and topics would be used without fetching:
    python3 ~/claude_memory/scripts/research_scout.py --list-topics

Limit results written per run (default: 25):
    python3 ~/claude_memory/scripts/research_scout.py --max-results 10

SOURCES
-------
  Ring 1 (direct): queries built from active beliefs and open questions.
  Ring 2 (adjacent): Qwen-expanded topics cached in cache/ring2_topics.json,
                     refreshed weekly.

  Sources queried:
    - PubMed              (academic, biomedical + life sciences)
    - arXiv               (preprint, CS / AI / physics / math / cog-sci)
    - OpenAlex            (academic, all disciplines -- broadest coverage)
    - Quanta RSS          (journalism, math + physics + biology + CS)
    - Trusted YouTube     (curated channels from trusted_sources table; channel
                           IDs resolved on first run and cached back to DB)

RELEVANCE FILTERING
-------------------
Each result abstract is embedded via nomic-embed-text and scored against
all existing memory_chunks. Results below RELEVANCE_THRESHOLD are dropped.
Daily ingest is capped at MAX_RESULTS, ranked by score.

DEDUPLICATION
-------------
DOI-based for academic sources. URL-based for RSS sources.
Already-seen records are silently skipped.

API KEYS
--------
NCBI_API_KEY is read from ~/claude_memory/.env. Without it the PubMed
querier falls back to 3 req/s (still functional, just slower).
"""

import argparse
import json
import os
import re
import sqlite3
import struct
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

_BASE       = Path.home() / "claude_memory"
DB_PATH     = _BASE / "memory.db"
ENV_PATH    = _BASE / ".env"
CACHE_DIR   = _BASE / "cache"
LOGS_DIR    = _BASE / "logs"
RING2_CACHE = CACHE_DIR / "ring2_topics.json"

# ── Config ─────────────────────────────────────────────────────────────────────

RELEVANCE_THRESHOLD    = 0.65   # Minimum cosine similarity to memory_chunks
MAX_RESULTS            = 25     # Max records written per day
RING2_CACHE_DAYS       = 7      # Refresh ring-2 topic expansion weekly
LOG_RETAIN_DAYS        = 14
RESULTS_PER_QUERY      = 5      # Results to fetch per source per query
YOUTUBE_PER_CHANNEL    = 5      # Recent videos to check per trusted channel

OLLAMA_BASE         = "http://localhost:11434"

# Read contact email for polite API use (OpenAlex mailto param).
# Set EMAIL in ~/.claude_memory/.ember_config to identify your requests to API providers.
def _read_config_email() -> str:
    config = _BASE / ".ember_config"
    if config.exists():
        for line in config.read_text().splitlines():
            line = line.strip()
            if line.startswith("EMAIL=") and not line.startswith("#"):
                val = line.split("=", 1)[1].strip()
                if val:
                    return val
    return "user@example.com"

CONTACT_EMAIL = _read_config_email()
EMBED_MODEL         = "nomic-embed-text"
EXPAND_MODEL        = "qwen2.5:14b"

# ── Logging ───────────────────────────────────────────────────────────────────

class Logger:
    def __init__(self, path, dry_run=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")
        self.dry_run = dry_run

    def write(self, msg=""):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        self._f.write(line + "\n")
        self._f.flush()

    def sep(self):
        self.write("=" * 60)

    def close(self):
        self._f.close()

    def prune(self):
        cutoff = datetime.now() - timedelta(days=LOG_RETAIN_DAYS)
        for f in LOGS_DIR.glob("scout_*.log"):
            try:
                date_str = f.stem.replace("scout_", "")
                if datetime.strptime(date_str, "%Y-%m-%d") < cutoff:
                    f.unlink()
            except Exception:
                pass


# ── Environment ────────────────────────────────────────────────────────────────

def load_env():
    """Load key=value pairs from .env file."""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Seed Generation: Ring 1 ───────────────────────────────────────────────────

# Terms that cause PubMed/arXiv to return hardware papers instead of AI papers.
# Deliberately narrow: only terms confirmed to cause false positives.
_HARDWARE_AMBIGUOUS = {"memory", "architecture"}
_AI_QUALIFIER = "artificial intelligence"

# ── False-positive filters ────────────────────────────────────────────────────
# Appended to every PubMed query to restrict results to CS/AI literature.
# PubMed is a medical database; without this, clinical AI papers dominate.
_PUBMED_CS_FILTER = (
    "AND (machine learning OR deep learning OR \"large language model\" "
    "OR \"language model\" OR transformer OR LLM OR \"neural network\" "
    "OR \"AI agent\" OR \"software agent\" OR embedding OR Ollama)"
)

# Minimum cosine similarity for PubMed results — higher than general threshold
# because clinical false positives score deceptively high.
PUBMED_RELEVANCE_THRESHOLD = 0.73

# Post-fetch content filter: keywords that indicate hardware memory context.
# If ANY of these appear in title+abstract and NO AI keyword is present, reject.
_HW_BLOCKLIST = {
    "rram", "dram", "sram", "flash memory", "chiplet", "semiconductor",
    "circuit board", "fpga", "memory cell", "error correcting code",
    "resistive memory", "memory-bound application", "mechanical computing",
    "associative memory system", "room impulse response",
}

# Post-fetch content filter: keywords that indicate clinical/medical context.
# If ANY of these appear and no strong AI/CS signal is present, reject.
_CLINICAL_BLOCKLIST = {
    "clinical trial", "randomized controlled", "cohort study",
    "patients with", "health care provider", "electronic health record",
    "primary health care", "motor skill", "wearable sensor",
    "brain-computer interface", "neurophysiolog",
    "bdnf", "alzheimer", "diabetes mellitus", "musculoskeletal",
    "fnirs", "transcranial", "animal research",
}

# Strong AI/CS signal — presence of ANY of these means the paper is relevant
# even if clinical or hardware terms also appear.
_AI_STRONG_SIGNAL = {
    "large language model", "language model", "llm", "machine learning",
    "deep learning", "transformer", "embedding model", "vector database",
    "retrieval-augmented", "ai agent", "autonomous agent", "fine-tun",
    "generative ai", "foundation model", "ollama", "gpt-", "claude ",
    "semantic memory system", "cognitive architecture",
}


def _is_domain_relevant(title: str, abstract: str, source_name: str) -> bool:
    """
    Post-fetch content filter. Returns False if the result is likely a
    hardware memory or clinical paper with no meaningful AI/CS content.

    Rules (applied in order):
      1. Hardware blocklist hit + no strong AI signal -> reject
      2. Clinical blocklist hit + no strong AI signal -> reject
      3. PubMed source + no AI keyword at all -> reject
    """
    combined = (title + " " + abstract).lower()
    has_strong_ai = any(k in combined for k in _AI_STRONG_SIGNAL)

    # Rule 1: hardware context without AI signal
    if any(k in combined for k in _HW_BLOCKLIST) and not has_strong_ai:
        return False

    # Rule 2: clinical context without AI signal
    if any(k in combined for k in _CLINICAL_BLOCKLIST) and not has_strong_ai:
        return False

    # Rule 3: PubMed must contain at least one AI keyword
    if source_name == "PubMed":
        ai_keywords = _AI_STRONG_SIGNAL | {
            "artificial intelligence", "neural network", "natural language",
            "computer vision", "speech recognition", "knowledge graph",
        }
        if not any(k in combined for k in ai_keywords):
            return False

    return True

def shorten_query(text, max_words=8):
    """
    Trim a verbose belief/question string to a clean keyword query suitable
    for PubMed / arXiv term search.

    For "topic_slug: long description" beliefs, extracts the slug as the
    primary term and appends a few distinctive words from the description
    (filtering out words already in the slug to avoid repetition).

    Appends "artificial intelligence" if the query contains hardware-ambiguous
    terms (memory, architecture) without an existing AI context signal.
    """
    if ": " in text:
        label, rest = text.split(": ", 1)
        label = label.replace("_", " ").strip()
        label_words_lower = {w.lower() for w in label.split()}
        # Distinctive words from description, not already in the label
        rest_words = [
            w for w in rest.split()
            if len(w) > 4 and w.lower() not in label_words_lower
        ][:4]
        combined = label + (" " + " ".join(rest_words) if rest_words else "")
    else:
        words = text.split()
        combined = " ".join(words[:max_words])

    combined = combined[:120]

    # Add AI qualifier if query has hardware-ambiguous terms but no AI context
    combined_lower = combined.lower()
    has_ambiguous = any(t in combined_lower for t in _HARDWARE_AMBIGUOUS)
    has_ai_context = any(t in combined_lower for t in
                         ("artificial intelligence", "machine learning", "neural",
                          "language model", "embedding", "llm", "agent", "ollama"))
    if has_ambiguous and not has_ai_context:
        combined = combined + " " + _AI_QUALIFIER

    return combined


def derive_ring1_seeds(conn, log):
    """
    Pull active beliefs and open questions from the database.
    Returns a list of (description, triggered_by_label) tuples.
    """
    seeds = []

    # Top beliefs by importance + confidence
    beliefs = conn.execute("""
        SELECT topic, position, importance_score, confidence_score
        FROM beliefs
        WHERE is_active = 1 OR is_active IS NULL
        ORDER BY (COALESCE(importance_score, 0.5) + COALESCE(confidence_score, 0.5)) DESC
        LIMIT 8
    """).fetchall()
    for b in beliefs:
        text = (b["topic"] or "") + (": " + b["position"] if b["position"] else "")
        if text:
            seeds.append((text[:200], f"belief: {text[:60]}"))

    # Most recent open questions
    questions = conn.execute("""
        SELECT question
        FROM questions
        WHERE status = 'open'
        ORDER BY id DESC
        LIMIT 5
    """).fetchall()
    for q in questions:
        text = q["question"]
        if text:
            seeds.append((text[:200], f"question: {text[:60]}"))

    log.write(f"  Ring 1: {len(seeds)} seeds from database")
    return seeds


# ── Seed Generation: Ring 2 ───────────────────────────────────────────────────

def load_ring2_cache():
    if RING2_CACHE.exists():
        try:
            data = json.loads(RING2_CACHE.read_text())
            generated = datetime.fromisoformat(data.get("generated", "2000-01-01"))
            if datetime.now() - generated < timedelta(days=RING2_CACHE_DAYS):
                return data.get("topics", [])
        except Exception:
            pass
    return None


def save_ring2_cache(topics):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RING2_CACHE.write_text(json.dumps({
        "generated": datetime.now().isoformat(),
        "topics": topics
    }, indent=2))


def expand_ring2_via_qwen(ring1_seeds, log):
    """
    Ask Qwen to generate adjacent research topics from the ring-1 seeds.
    Returns a list of (topic_string, triggered_by_label) tuples.
    """
    core_topics = [s[0][:120] for s in ring1_seeds[:10]]
    topics_text = "\n".join(f"- {t}" for t in core_topics)

    prompt = (
        "You are a research librarian specializing in computer science and AI. "
        "Given the following research topics that an AI systems developer is actively "
        "thinking about, identify 8 to 12 closely adjacent academic fields or research "
        "areas from computer science, AI, and software engineering that are NOT already "
        "listed but would naturally be relevant as the work evolves.\n\n"
        "IMPORTANT: Stay within computer science, AI, machine learning, and software "
        "engineering. Do NOT suggest neuroscience, clinical medicine, biology, or "
        "hardware engineering topics — these generate false positives in academic "
        "literature searches.\n\n"
        "Good examples: persistent memory systems, continual learning, episodic memory "
        "in LLMs, knowledge graph reasoning, agent communication protocols.\n"
        "Bad examples: neural plasticity, brain-computer interfaces, Alzheimer's "
        "disease, DRAM architecture — these are out of scope.\n\n"
        "Focus on areas that would surface peer-reviewed CS/AI literature with different "
        "perspectives, methods, or evidence than the core topics. Be specific.\n\n"
        f"Core topics:\n{topics_text}\n\n"
        "Return ONLY a JSON array of short topic strings. "
        'Example: ["continual learning in neural networks", "episodic memory for LLMs", '
        '"knowledge distillation techniques"]\n\n'
        "Adjacent CS/AI topics:"
    )

    payload = json.dumps({
        "model": EXPAND_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_ctx": 4096}
    }).encode()

    try:
        req = urllib.request.Request(
            f"{OLLAMA_BASE}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            raw = result.get("response", "").strip()

        # Extract JSON array from response
        match = re.search(r'\[.*?\]', raw, re.DOTALL)
        if match:
            topics = json.loads(match.group())
            if isinstance(topics, list) and topics:
                log.write(f"  Ring 2: Qwen generated {len(topics)} adjacent topics")
                return [(t, f"ring2_expansion: {t}") for t in topics if isinstance(t, str)]
    except Exception as e:
        log.write(f"  Ring 2: Qwen expansion failed ({e}), skipping")

    return []


def derive_ring2_seeds(ring1_seeds, log, force_refresh=False):
    """
    Load ring-2 topics from cache, or regenerate via Qwen.
    Returns list of (topic_string, triggered_by_label) tuples.
    """
    if not force_refresh:
        cached = load_ring2_cache()
        if cached is not None:
            log.write(f"  Ring 2: loaded {len(cached)} topics from cache (fresh)")
            return [(t, f"ring2_expansion: {t}") for t in cached]
        log.write("  Ring 2: cache stale or missing, regenerating via Qwen...")
    else:
        log.write("  Ring 2: forced refresh via Qwen...")

    topics_with_labels = expand_ring2_via_qwen(ring1_seeds, log)
    topic_strings = [t[0] for t in topics_with_labels]
    save_ring2_cache(topic_strings)
    return topics_with_labels


# ── Embedding and Similarity ──────────────────────────────────────────────────

def embed_text(text):
    """Return a 768-dim float list via nomic-embed-text, or None on failure."""
    payload = json.dumps({
        "model": EMBED_MODEL,
        "prompt": text[:2000]
    }).encode()
    try:
        req = urllib.request.Request(
            f"{OLLAMA_BASE}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result.get("embedding")
    except Exception:
        return None


def cosine_similarity(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def unpack_vector(blob):
    if not blob:
        return None
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def load_memory_chunks(conn):
    """Load all embedded memory chunks for scoring."""
    rows = conn.execute(
        "SELECT id, embedding_vector FROM memory_chunks WHERE embedding_vector IS NOT NULL"
    ).fetchall()
    return [(r["id"], unpack_vector(r["embedding_vector"])) for r in rows if r["embedding_vector"]]


def score_against_memory(abstract, chunks):
    """
    Embed the abstract and return the max cosine similarity against all chunks.
    Returns (score, None) if Ollama is unavailable.
    """
    if not abstract or not chunks:
        return 0.0
    vec = embed_text(abstract)
    if vec is None:
        return None  # Ollama offline
    scores = [cosine_similarity(vec, chunk_vec) for _, chunk_vec in chunks]
    return max(scores) if scores else 0.0


# ── Deduplication ─────────────────────────────────────────────────────────────

def already_seen(conn, doi=None, url=None):
    """Return True if this DOI or URL is already in scout_results."""
    if doi:
        row = conn.execute(
            "SELECT id FROM scout_results WHERE doi = ?", (doi,)
        ).fetchone()
        if row:
            return True
    if url:
        row = conn.execute(
            "SELECT id FROM scout_results WHERE source_url = ?", (url,)
        ).fetchone()
        if row:
            return True
    return False


# ── Source: PubMed ────────────────────────────────────────────────────────────

def fetch_pubmed(query, api_key=None, max_results=RESULTS_PER_QUERY, log=None):
    """
    Search PubMed via E-utilities. Returns list of candidate dicts.
    """
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
        "sort": "relevance"
    }
    if api_key:
        params["api_key"] = api_key

    candidates = []
    try:
        # Step 1: search for IDs
        search_url = base + "esearch.fcgi?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(search_url, timeout=15) as resp:
            data = json.loads(resp.read())
        ids = data.get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []

        time.sleep(0.15 if api_key else 0.35)

        # Step 2: fetch summaries
        fetch_params = {
            "db": "pubmed",
            "id": ",".join(ids),
            "retmode": "xml",
            "rettype": "abstract"
        }
        if api_key:
            fetch_params["api_key"] = api_key

        fetch_url = base + "efetch.fcgi?" + urllib.parse.urlencode(fetch_params)
        with urllib.request.urlopen(fetch_url, timeout=30) as resp:
            xml_data = resp.read()

        root = ET.fromstring(xml_data)
        for article in root.findall(".//PubmedArticle"):
            try:
                medline = article.find("MedlineCitation")
                art = medline.find("Article")

                title_el = art.find("ArticleTitle")
                title = "".join(title_el.itertext()) if title_el is not None else ""

                abstract_el = art.find("Abstract")
                abstract = ""
                if abstract_el is not None:
                    parts = abstract_el.findall("AbstractText")
                    abstract = " ".join("".join(p.itertext()) for p in parts)

                # Authors
                authors = []
                author_list = art.find("AuthorList")
                if author_list is not None:
                    for a in author_list.findall("Author")[:5]:
                        last = a.findtext("LastName", "")
                        fore = a.findtext("ForeName", "")
                        if last:
                            authors.append(f"{fore} {last}".strip())

                # PMID and DOI
                pmid = medline.findtext("PMID", "")
                doi = ""
                for id_el in article.findall(".//ArticleId"):
                    if id_el.get("IdType") == "doi":
                        doi = id_el.text or ""
                        break

                # Publication date
                pub_date_el = art.find(".//PubDate")
                pub_date = ""
                if pub_date_el is not None:
                    year = pub_date_el.findtext("Year", "")
                    month = pub_date_el.findtext("Month", "")
                    pub_date = f"{year}-{month}" if month else year

                candidates.append({
                    "title": title.strip(),
                    "authors": json.dumps(authors),
                    "abstract": abstract.strip(),
                    "doi": doi.strip() or None,
                    "source_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    "source_name": "pubmed",
                    "source_type": "academic",
                    "publication_date": pub_date,
                    "external_id": pmid,
                })
            except Exception:
                continue

    except Exception as e:
        if log:
            log.write(f"    PubMed error: {e}")

    return candidates


# ── Source: arXiv ─────────────────────────────────────────────────────────────

def fetch_arxiv(query, max_results=RESULTS_PER_QUERY, log=None):
    """
    Search arXiv via the public API. Returns list of candidate dicts.
    Returns None (not []) on rate-limit (HTTP 429) so the caller can back off.
    arXiv asks for >= 3 seconds between requests; caller must enforce this.
    """
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending"
    }
    url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    candidates = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "claude-memory-scout/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)

        for entry in root.findall("atom:entry", ns):
            try:
                title = entry.findtext("atom:title", "", ns).strip().replace("\n", " ")
                abstract = entry.findtext("atom:summary", "", ns).strip().replace("\n", " ")

                authors = []
                for a in entry.findall("atom:author", ns)[:5]:
                    name = a.findtext("atom:name", "", ns)
                    if name:
                        authors.append(name)

                arxiv_id_url = entry.findtext("atom:id", "", ns).strip()
                arxiv_id = arxiv_id_url.split("/abs/")[-1] if "/abs/" in arxiv_id_url else ""

                doi = None
                doi_el = entry.find(
                    "{http://arxiv.org/schemas/atom}doi"
                )
                if doi_el is not None and doi_el.text:
                    doi = doi_el.text.strip()

                pub_date = entry.findtext("atom:published", "", ns)[:10]

                candidates.append({
                    "title": title,
                    "authors": json.dumps(authors),
                    "abstract": abstract,
                    "doi": doi,
                    "source_url": f"https://arxiv.org/abs/{arxiv_id}",
                    "source_name": "arxiv",
                    "source_type": "preprint",
                    "publication_date": pub_date,
                    "external_id": arxiv_id,
                })
            except Exception:
                continue

    except urllib.error.HTTPError as e:
        if e.code == 429:
            if log:
                log.write(f"    arXiv rate-limited (429) -- will retry after backoff")
            return None   # sentinel: caller should back off, then retry
        if log:
            log.write(f"    arXiv error: {e}")
    except Exception as e:
        if log:
            log.write(f"    arXiv error: {e}")

    return candidates


# ── Source: OpenAlex ──────────────────────────────────────────────────────────

def fetch_openalex(query, max_results=RESULTS_PER_QUERY, log=None):
    """
    Search OpenAlex via the public API. Returns list of candidate dicts.
    Politely identifies the request via the mailto param.
    """
    params = {
        "search": query,
        "per-page": max_results,
        "select": "id,title,authorships,abstract_inverted_index,doi,publication_date,primary_location",
        "sort": "relevance_score:desc",
        "mailto": CONTACT_EMAIL
    }
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    candidates = []

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ClaudeMemoryScout/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())

        for work in data.get("results", []):
            try:
                title = work.get("title", "").strip()

                # Reconstruct abstract from inverted index
                inv = work.get("abstract_inverted_index")
                abstract = ""
                if inv:
                    positions = {}
                    for word, locs in inv.items():
                        for pos in locs:
                            positions[pos] = word
                    abstract = " ".join(positions[k] for k in sorted(positions))

                authors = []
                for auth in work.get("authorships", [])[:5]:
                    name = auth.get("author", {}).get("display_name", "")
                    if name:
                        authors.append(name)

                doi_raw = work.get("doi", "") or ""
                doi = doi_raw.replace("https://doi.org/", "").strip() or None

                pub_date = work.get("publication_date", "")
                openalex_id = work.get("id", "").replace("https://openalex.org/", "")

                source_url = doi_raw if doi_raw else f"https://openalex.org/{openalex_id}"

                candidates.append({
                    "title": title,
                    "authors": json.dumps(authors),
                    "abstract": abstract,
                    "doi": doi,
                    "source_url": source_url,
                    "source_name": "openalex",
                    "source_type": "academic",
                    "publication_date": pub_date,
                    "external_id": openalex_id,
                })
            except Exception:
                continue

    except Exception as e:
        if log:
            log.write(f"    OpenAlex error: {e}")

    return candidates


# ── Source: Quanta RSS ────────────────────────────────────────────────────────

def fetch_quanta(log=None):
    """
    Fetch the Quanta Magazine RSS feed and return recent items.
    Topic filtering happens at the relevance scoring stage.
    """
    url = "https://www.quantamagazine.org/feed/"
    candidates = []

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "ClaudeMemoryScout/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_data = resp.read()

        root = ET.fromstring(xml_data)
        ns = {"content": "http://purl.org/rss/1.0/modules/content/"}

        items = root.findall(".//item")[:15]
        for item in items:
            try:
                title = item.findtext("title", "").strip()
                link  = item.findtext("link", "").strip()
                desc  = item.findtext("description", "").strip()
                pub_date = item.findtext("pubDate", "")[:16]

                # Strip HTML tags from description
                desc = re.sub(r"<[^>]+>", "", desc).strip()

                candidates.append({
                    "title": title,
                    "authors": json.dumps([]),
                    "abstract": desc,
                    "doi": None,
                    "source_url": link,
                    "source_name": "quanta",
                    "source_type": "journalism",
                    "publication_date": pub_date,
                    "external_id": link,
                })
            except Exception:
                continue

    except Exception as e:
        if log:
            log.write(f"    Quanta RSS error: {e}")

    return candidates


# ── Source: Trusted YouTube Channels ─────────────────────────────────────────

def resolve_channel_id(channel_url: str, log, video_id_hint: str = None) -> str:
    """
    Extract a YouTube channel ID (UC...) from a channel URL.

    Resolution order (stops at first success):
      1. Direct /channel/UCxxxxxxxx URL — regex parse, no network.
      2. oEmbed lookup via a known video_id — lightweight API call,
         no auth needed. Returns author_url in /channel/UC... format
         for most channels. Pass video_id_hint from trusted_sources.notes.
      3. Page scrape — last resort; YouTube blocks this for @handle URLs
         in most environments but kept as a fallback.

    Returns the UC... string or None on failure.
    """
    if not channel_url:
        return None

    # 1. Direct /channel/ URL — no fetch needed
    m = re.search(r"/channel/(UC[\w-]+)", channel_url)
    if m:
        return m.group(1)

    # 2. oEmbed via known video_id — preferred fallback, no bot-detection issues
    if video_id_hint:
        ch_id = _resolve_via_oembed(video_id_hint, log)
        if ch_id:
            return ch_id

    # 3. Page scrape — @handle URL (often blocked, kept as last resort)
    try:
        req = urllib.request.Request(
            channel_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClaudeMemoryScout/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")[:80000]

        for pattern in [
            r'"externalId"\s*:\s*"(UC[\w-]+)"',
            r'"channelId"\s*:\s*"(UC[\w-]+)"',
            r'channel_id=(UC[\w-]+)',
        ]:
            m = re.search(pattern, html)
            if m:
                return m.group(1)
    except Exception as e:
        if log:
            log.write(f"    page scrape failed for {channel_url}: {e}")

    return None


def _resolve_via_oembed(video_id: str, log) -> str:
    """
    Call the YouTube oEmbed API with a known video_id to get the channel_id.

    oEmbed returns author_url in /channel/UCxxxxxxxx format for most channels.
    No API key required. Respects YouTube's terms (read-only metadata, no scraping).

    Returns UC... string or None.
    """
    url = (f"https://www.youtube.com/oembed"
           f"?url=https://www.youtube.com/watch?v={video_id}&format=json")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ClaudeMemoryScout/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        author_url = data.get("author_url", "")
        m = re.search(r"/channel/(UC[\w-]+)", author_url)
        if m:
            return m.group(1)
        if log:
            log.write(f"    oEmbed: author_url={author_url} (no UC... found)")
    except Exception as e:
        if log:
            log.write(f"    oEmbed failed for video {video_id}: {e}")
    return None


def fetch_youtube_rss(channel_id: str, channel_name: str,
                      max_videos: int, log) -> list:
    """
    Fetch the YouTube channel RSS feed and return recent video entries.

    YouTube provides a public Atom feed at:
        https://www.youtube.com/feeds/videos.xml?channel_id=UC...

    Each entry includes title, publication date, and a description snippet via
    media:description — enough for relevance scoring without fetching individual
    transcripts.

    Returns a list of dicts: title, description, url, video_id, published.
    """
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    videos = []

    ns = {
        "atom":  "http://www.w3.org/2005/Atom",
        "yt":    "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }

    try:
        req = urllib.request.Request(
            rss_url,
            headers={"User-Agent": "ClaudeMemoryScout/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_data = resp.read()

        root = ET.fromstring(xml_data)

        for entry in root.findall("atom:entry", ns)[:max_videos]:
            try:
                title = entry.findtext("atom:title", "", ns).strip()
                published = entry.findtext("atom:published", "", ns)[:10]

                video_id_el = entry.find("yt:videoId", ns)
                video_id = video_id_el.text.strip() if video_id_el is not None else ""
                if not video_id:
                    continue

                url = f"https://www.youtube.com/watch?v={video_id}"

                desc = ""
                group = entry.find("media:group", ns)
                if group is not None:
                    desc_el = group.find("media:description", ns)
                    if desc_el is not None and desc_el.text:
                        desc = desc_el.text[:1500].strip()

                if title:
                    videos.append({
                        "title":       title,
                        "description": desc,
                        "url":         url,
                        "video_id":    video_id,
                        "published":   published,
                    })
            except Exception:
                continue

    except Exception as e:
        if log:
            log.write(f"    YouTube RSS error for {channel_name}: {e}")

    return videos


def _extract_video_id_from_notes(notes: str) -> str:
    """Pull the first YouTube video ID (11 chars) from a trusted_sources notes field."""
    if not notes:
        return None
    m = re.search(r'Videos?:\s*([a-zA-Z0-9_-]{11})', notes)
    return m.group(1) if m else None


def fetch_trusted_youtube_channels(conn, log, max_per_channel: int = YOUTUBE_PER_CHANNEL) -> list:
    """
    Pull recent videos from all active channels in the trusted_sources table.

    On first run, channel_ids (UC...) are resolved using:
      1. oEmbed API via a known video_id from trusted_sources.notes (preferred)
      2. Page scrape fallback
    Resolved IDs are cached back to trusted_sources.channel_id so subsequent
    runs skip the lookup entirely.

    Returns a list of candidate dicts ready for relevance scoring and writing
    to scout_results.
    """
    rows = conn.execute("""
        SELECT id, channel_name, channel_url, channel_id, topic_focus, notes
        FROM trusted_sources
        WHERE is_active = 1 AND source_type = 'youtube_channel'
        ORDER BY id
    """).fetchall()

    if not rows:
        return []

    log.write(f"  [Trusted YouTube] {len(rows)} active channel(s)")
    candidates = []

    for row in rows:
        ch_id    = row["channel_id"]
        ch_name  = row["channel_name"] or "Unknown"
        ch_url   = row["channel_url"] or ""
        ch_db_id = row["id"]
        notes    = row["notes"] or ""

        # Resolve and cache channel_id on first encounter
        if not ch_id:
            video_hint = _extract_video_id_from_notes(notes)
            log.write(f"    Resolving channel_id for {ch_name}"
                      + (f" (hint: {video_hint})" if video_hint else "") + "...")
            ch_id = resolve_channel_id(ch_url, log, video_id_hint=video_hint)
            if ch_id:
                conn.execute(
                    "UPDATE trusted_sources SET channel_id = ? WHERE id = ?",
                    (ch_id, ch_db_id)
                )
                conn.commit()
                log.write(f"    Resolved and cached: {ch_id}")
            else:
                log.write(f"    Could not resolve channel_id for {ch_name} — skipping")
                continue
            time.sleep(1)  # polite pause between page fetches

        # Fetch RSS feed for this channel
        log.write(f"    [{ch_name}] fetching RSS...")
        videos = fetch_youtube_rss(ch_id, ch_name, max_per_channel, log)
        log.write(f"    {len(videos)} recent video(s) found")

        for v in videos:
            if already_seen(conn, doi=None, url=v["url"]):
                continue
            candidates.append({
                "title":            v["title"],
                "authors":          json.dumps([ch_name]),
                "abstract":         v["description"],
                "doi":              None,
                "source_url":       v["url"],
                "source_name":      ch_name,
                "source_type":      "youtube_channel",
                "publication_date": v["published"],
                "external_id":      v["video_id"],
            })

        time.sleep(0.5)  # polite pause between channels

    log.write(f"  [Trusted YouTube] {len(candidates)} new candidate(s) after dedup")
    return candidates


# ── Writer ────────────────────────────────────────────────────────────────────

def write_result(conn, candidate, score, query, ring, triggered_by, dry_run=False):
    """Insert one result into scout_results. Returns True if written."""
    if dry_run:
        return True

    try:
        conn.execute("""
            INSERT INTO scout_results (
                title, authors, abstract, doi, source_url,
                source_name, source_type, publication_date, external_id,
                date_fetched, search_query, search_ring, triggered_by,
                relevance_score, status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,date('now'),?,?,?,?,
                      'pending', datetime('now'), datetime('now'))
        """, (
            candidate["title"],
            candidate["authors"],
            candidate["abstract"],
            candidate["doi"],
            candidate["source_url"],
            candidate["source_name"],
            candidate["source_type"],
            candidate["publication_date"],
            candidate["external_id"],
            query[:300] if query else None,
            ring,
            triggered_by[:200] if triggered_by else None,
            round(score, 4) if score is not None else None,
        ))
        conn.commit()
        return True
    except Exception as e:
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def run(args):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    log_path = LOGS_DIR / f"scout_{datetime.now().strftime('%Y-%m-%d')}.log"
    log = Logger(log_path, dry_run=args.dry_run)
    log.prune()

    log.sep()
    log.write("Research Scout Agent")
    if args.dry_run:
        log.write("Mode: DRY RUN (nothing will be written)")
    log.sep()
    log.write("")

    # Load environment
    env = load_env()
    ncbi_api_key = env.get("NCBI_API_KEY")
    if ncbi_api_key:
        log.write(f"NCBI API key: loaded")
    else:
        log.write("NCBI API key: not found (using 3 req/s rate limit)")

    # Database
    conn = get_db()
    if conn is None:
        log.write("FATAL: Database not found. Run setup_db.py first.")
        log.close()
        return

    # Check scout_results table exists
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scout_results'"
    ).fetchone()
    if not row:
        log.write("FATAL: scout_results table not found.")
        log.write("Run migrate_add_scout_results.py first.")
        log.close()
        return

    # Load memory chunks for scoring
    log.write("Loading memory chunks for relevance scoring...")
    chunks = load_memory_chunks(conn)
    log.write(f"  {len(chunks)} chunks loaded")
    ollama_online = len(chunks) > 0 and embed_text("test") is not None
    if not ollama_online:
        log.write("  Ollama offline -- relevance scoring disabled. Using keyword fallback.")
    log.write("")

    # Generate seeds
    log.write("Generating topic seeds...")
    ring1 = derive_ring1_seeds(conn, log)
    ring2 = derive_ring2_seeds(ring1, log, force_refresh=args.refresh_ring2)
    all_seeds = [(q, lbl, 1) for q, lbl in ring1] + [(q, lbl, 2) for q, lbl in ring2]
    log.write(f"  Total seeds: {len(all_seeds)} ({len(ring1)} ring-1, {len(ring2)} ring-2)")
    log.write("")

    if args.list_topics:
        log.write("Ring 1 seeds:")
        for q, lbl, _ in all_seeds[:len(ring1)]:
            log.write(f"  [R1] {q[:100]}")
        log.write("Ring 2 seeds:")
        for q, lbl, _ in all_seeds[len(ring1):]:
            log.write(f"  [R2] {q[:100]}")
        conn.close()
        log.close()
        return

    # Fetch and score candidates
    max_results = args.max_results
    all_candidates = []  # (score, candidate, query, ring, triggered_by)

    log.write("Fetching from sources...")
    log.write("")

    # Quanta RSS is not query-driven -- fetch once and score everything
    log.write("  [Quanta RSS]")
    quanta_items = fetch_quanta(log)
    log.write(f"    Fetched {len(quanta_items)} items")
    for item in quanta_items:
        if already_seen(conn, doi=item["doi"], url=item["source_url"]):
            continue
        text = f"{item['title']} {item['abstract']}"
        if ollama_online:
            score = score_against_memory(text, chunks)
        else:
            score = 0.0
        if score is None:
            score = 0.0
        if score >= RELEVANCE_THRESHOLD:
            all_candidates.append((score, item, "quanta_rss", 1, "quanta_feed"))
    log.write("")

    # Trusted YouTube channels — curated sources from trusted_sources table
    log.write("  [Trusted YouTube Channels]")
    yt_items = fetch_trusted_youtube_channels(conn, log)
    for item in yt_items:
        text = f"{item['title']} {item['abstract']}"
        if ollama_online:
            score = score_against_memory(text, chunks)
        else:
            score = 0.0
        if score is None:
            score = 0.0
        triggered_by = f"trusted_source: {item['source_name']}"
        if score >= RELEVANCE_THRESHOLD:
            all_candidates.append((score, item, item["source_name"], 1, triggered_by))
    log.write("")

    # Query-driven sources
    sources = [
        ("PubMed",    fetch_pubmed,    {"api_key": ncbi_api_key, "log": log}),
        ("arXiv",     fetch_arxiv,     {"log": log}),
        ("OpenAlex",  fetch_openalex,  {"log": log}),
    ]

    for seed_query, triggered_by, ring in all_seeds:
        for source_name, fetch_fn, extra_kwargs in sources:
            log.write(f"  [{source_name}] ring={ring} | {seed_query[:70]}...")
            # PubMed and arXiv work better with short keyword terms than verbose
            # belief descriptions. OpenAlex handles natural language well.
            if source_name == "PubMed":
                api_query = shorten_query(seed_query) + " " + _PUBMED_CS_FILTER
            elif source_name == "arXiv":
                api_query = shorten_query(seed_query)
            else:
                api_query = seed_query
            try:
                # arXiv: enforce 4s minimum between calls; backoff on 429
                if source_name == "arXiv":
                    time.sleep(10)  # arXiv rate limit: be conservative, 429s seen at 4s
                    items = fetch_fn(api_query, **extra_kwargs)
                    if items is None:  # rate-limited
                        time.sleep(30)
                        items = fetch_fn(api_query, **extra_kwargs) or []
                elif source_name == "PubMed":
                    items = fetch_fn(api_query, **extra_kwargs)
                else:
                    items = fetch_fn(seed_query, **extra_kwargs)
            except Exception as e:
                log.write(f"    Error: {e}")
                continue

            new_count = 0
            src_threshold = PUBMED_RELEVANCE_THRESHOLD if source_name == "PubMed" else RELEVANCE_THRESHOLD
            for item in items:
                if already_seen(conn, doi=item["doi"], url=item["source_url"]):
                    continue
                # Content filter: reject hardware/clinical false positives
                if not _is_domain_relevant(item["title"] or "", item["abstract"] or "", source_name):
                    continue
                text = f"{item['title']} {item['abstract']}"
                if ollama_online:
                    score = score_against_memory(text, chunks)
                else:
                    score = 0.0
                if score is None:
                    score = 0.0
                if score >= src_threshold:
                    all_candidates.append((score, item, seed_query, ring, triggered_by))
                    new_count += 1

            log.write(f"    {len(items)} fetched, {new_count} above threshold")
            time.sleep(0.1)

    # ── Ring 3: pending research_tasks from belief_checksum ──────────────────
    # The checksum mechanism queues beliefs that lack external coverage.
    # We consume those here as targeted search queries, closing the loop
    # the founding conversation described: belief extracted → checksum fires
    # → scout hunts for grounding → belief gets verified or challenged.
    pending_tasks = conn.execute("""
        SELECT id, query FROM research_tasks
        WHERE status = 'pending' AND source_type = 'checksum'
        ORDER BY confidence_score DESC, created_at ASC
        LIMIT 20
    """).fetchall()

    if pending_tasks:
        log.write(f"  [Ring 3 — Belief Checksum Queue] {len(pending_tasks)} pending task(s)")
        fulfilled_task_ids = []

        for task_id, task_query in pending_tasks:
            if not task_query:
                continue
            short_q = task_query[:100].strip()
            task_new = 0

            for fetcher, src_name, max_r in [
                (fetch_arxiv,    "arxiv",    2),
                (fetch_pubmed,   "pubmed",   2),
                (fetch_openalex, "openalex", 2),
            ]:
                try:
                    if src_name == "pubmed":
                        items = fetcher(short_q, api_key=ncbi_api_key,
                                        max_results=max_r, log=log)
                    else:
                        items = fetcher(short_q, max_results=max_r, log=log)
                except Exception:
                    items = []
                items = items or []   # guard: fetcher may return None on 429/timeout

                for item in items:
                    if already_seen(conn, doi=item.get("doi"),
                                    url=item.get("source_url")):
                        continue
                    text = f"{item['title']} {item.get('abstract','')}"
                    score = score_against_memory(text, chunks) if ollama_online else 0.55
                    if score is None:
                        score = 0.55
                    all_candidates.append(
                        (score, item, short_q, "ring3",
                         f"research_task:{task_id}")
                    )
                    task_new += 1

                time.sleep(0.1)

            if task_new > 0:
                fulfilled_task_ids.append(task_id)

        # Mark fulfilled tasks (found at least one candidate)
        if fulfilled_task_ids and not args.dry_run:
            placeholders = ",".join("?" * len(fulfilled_task_ids))
            conn.execute(
                f"UPDATE research_tasks SET status='fulfilled' "
                f"WHERE id IN ({placeholders})",
                fulfilled_task_ids,
            )
            conn.commit()

        still_pending = len(pending_tasks) - len(fulfilled_task_ids)
        log.write(f"    {len(fulfilled_task_ids)} task(s) fulfilled, "
                  f"{still_pending} still pending")
        log.write("")

    log.write("")

    # Deduplicate candidates by DOI / URL across all sources (highest score wins)
    seen_keys = set()
    deduped = []
    for score, item, query, ring, triggered_by in sorted(all_candidates, key=lambda x: x[0], reverse=True):
        key = item.get("doi") or item.get("source_url")
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        deduped.append((score, item, query, ring, triggered_by))

    # Cap at max_results, ranked by score
    to_write = deduped[:max_results]

    log.write(f"Results: {len(all_candidates)} raw hits -> {len(deduped)} unique -> "
              f"{len(to_write)} to write (cap: {max_results})")
    log.write("")

    # Write to database
    written = 0
    if to_write:
        log.write("Writing to scout_results...")
        for score, item, query, ring, triggered_by in to_write:
            ok = write_result(conn, item, score, query, ring, triggered_by,
                              dry_run=args.dry_run)
            if ok:
                written += 1
                marker = "[DRY RUN] " if args.dry_run else ""
                log.write(f"  {marker}[{item['source_name']}] score={score:.3f} "
                          f"| {item['title'][:70]}")

    log.write("")
    log.sep()
    log.write(f"Done. {written} result(s) written to scout_results.")
    if written > 0 and not args.dry_run:
        log.write("Review pending results:")
        log.write("  python3 ~/claude_memory/scripts/review_scout.py")
    log.sep()

    conn.close()
    log.close()


def _get_top_beliefs_for_contrary(conn, limit=15):
    """
    Return high-confidence verified/supported beliefs to generate
    contrary search queries against.
    """
    rows = conn.execute("""
        SELECT id, topic, position, confidence_score
        FROM beliefs
        WHERE is_active = 1
          AND status IN ('verified', 'supported')
          AND confidence_score >= 0.75
        ORDER BY confidence_score DESC, id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [{"id": r[0], "topic": r[1], "position": r[2], "score": r[3]}
            for r in rows]


def _build_contrary_queries(belief):
    """
    Generate search queries designed to surface content that challenges
    or contradicts a belief. Returns list of query strings.
    """
    topic    = (belief["topic"] or "").strip()
    position = (belief["position"] or "").strip()

    # Strip common filler from the position to get a clean searchable phrase
    core = position
    for prefix in ["The ", "A ", "An ", "It is ", "This is ", "We ", "Our "]:
        if core.startswith(prefix):
            core = core[len(prefix):]
    core = core[:120].rstrip(".")

    queries = []
    if topic:
        queries.append(f"limitations of {topic}")
        queries.append(f"criticism {topic}")
        queries.append(f"problems with {topic}")
    if core:
        queries.append(f"against {core[:80]}")
        queries.append(f"failure {core[:60]}")
    return queries[:3]   # cap at 3 queries per belief


def run_contrary(args):
    """
    Contrary perspectives pull.

    Designed to run monthly (separate launchd schedule). Reads the top
    verified beliefs, generates challenge queries, fetches from arxiv /
    PubMed / OpenAlex, and writes results tagged search_ring='contrary'
    into scout_results. These surface in review_scout.py alongside normal
    results and are labelled so the user knows they represent opposing views.

    Usage:
        python3 ~/claude_memory/scripts/research_scout.py --contrary
        python3 ~/claude_memory/scripts/research_scout.py --contrary --dry-run
        python3 ~/claude_memory/scripts/research_scout.py --contrary --no-jitter
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    log_path = LOGS_DIR / f"scout_contrary_{datetime.now().strftime('%Y-%m-%d')}.log"
    log = Logger(log_path, dry_run=args.dry_run)
    log.prune()

    log.sep()
    log.write("Research Scout — Contrary Perspectives Pull")
    log.write("Purpose: surface content that challenges existing high-confidence beliefs")
    if args.dry_run:
        log.write("Mode: DRY RUN")
    log.sep()
    log.write("")

    env = load_env()
    ncbi_api_key = env.get("NCBI_API_KEY")

    conn = get_db()
    if conn is None:
        log.write("FATAL: Database not found.")
        log.close()
        return

    beliefs = _get_top_beliefs_for_contrary(conn, limit=15)
    if not beliefs:
        log.write("No qualifying beliefs found (need verified/supported with conf >= 0.75).")
        conn.close()
        log.close()
        return

    log.write(f"Generating contrary queries for {len(beliefs)} belief(s)...\n")

    all_queries = []   # (query_str, triggered_by_label)
    for b in beliefs:
        queries = _build_contrary_queries(b)
        label = f"contrary:belief:{b['id']}"
        for q in queries:
            all_queries.append((q, label))
        log.write(f"  Belief {b['id']} [{b['score']:.2f}]: {(b['topic'] or '')[:50]}")
        for q in queries:
            log.write(f"    -> \"{q}\"")

    log.write(f"\nTotal contrary queries: {len(all_queries)}\n")

    written = 0
    seen    = set()

    for query, triggered_by in all_queries:
        log.write(f"  Query: \"{query}\"")

        # arXiv
        candidates = fetch_arxiv(query, max_results=3, log=log)
        for c in candidates:
            key = c.get("doi") or c.get("source_url","")
            if key in seen or already_seen(conn, doi=c.get("doi"), url=c.get("source_url")):
                continue
            seen.add(key)
            ok = write_result(conn, c, 0.65, query, "contrary", triggered_by,
                              dry_run=args.dry_run)
            if ok:
                written += 1
                log.write(f"    [arxiv] {c['title'][:80]}")

        # PubMed
        candidates = fetch_pubmed(query, api_key=ncbi_api_key, max_results=2, log=log)
        for c in candidates:
            key = c.get("doi") or c.get("source_url","")
            if key in seen or already_seen(conn, doi=c.get("doi"), url=c.get("source_url")):
                continue
            seen.add(key)
            ok = write_result(conn, c, 0.65, query, "contrary", triggered_by,
                              dry_run=args.dry_run)
            if ok:
                written += 1
                log.write(f"    [pubmed] {c['title'][:80]}")

        # OpenAlex
        candidates = fetch_openalex(query, max_results=2, log=log)
        for c in candidates:
            key = c.get("doi") or c.get("source_url","")
            if key in seen or already_seen(conn, doi=c.get("doi"), url=c.get("source_url")):
                continue
            seen.add(key)
            ok = write_result(conn, c, 0.65, query, "contrary", triggered_by,
                              dry_run=args.dry_run)
            if ok:
                written += 1
                log.write(f"    [openalex] {c['title'][:80]}")

    log.sep()
    log.write(f"Done. {written} contrary result(s) written.")
    log.write("These are tagged search_ring='contrary' in scout_results.")
    log.write("Review with: python3 ~/claude_memory/scripts/review_scout.py")
    log.sep()

    conn.close()
    log.close()


def parse_args():
    p = argparse.ArgumentParser(description="Research Scout Agent")
    p.add_argument("--dry-run",       action="store_true",
                   help="Fetch and score but do not write to database")
    p.add_argument("--refresh-ring2", action="store_true",
                   help="Force regeneration of ring-2 topic cache via Qwen")
    p.add_argument("--list-topics",   action="store_true",
                   help="Show seeds without fetching from any source")
    p.add_argument("--max-results",   type=int, default=MAX_RESULTS,
                   help=f"Max results to write per run (default: {MAX_RESULTS})")
    p.add_argument("--no-jitter",     action="store_true",
                   help="Skip startup jitter delay (use when running manually)")
    p.add_argument("--contrary",      action="store_true",
                   help="Run contrary perspectives pull (monthly mode): seek content "
                        "that challenges existing high-confidence beliefs")
    return p.parse_args()


if __name__ == "__main__":
    import random
    args = parse_args()
    if not args.no_jitter and not args.dry_run and not args.list_topics and not args.contrary:
        # Startup jitter: spread wake-triggered agents across a 5-minute window
        # so they don't all hammer Ollama simultaneously on machine wake.
        delay = random.randint(0, 300)
        import time
        time.sleep(delay)
    if args.contrary:
        run_contrary(args)
    else:
        run(args)
