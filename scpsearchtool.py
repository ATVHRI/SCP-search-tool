# scp_freq_search.py
# A minimal FastAPI service that crawls the SCP Wiki and ranks pages by how often a query appears in the article text.
# Focus: simple term/phrase frequency (not tags), polite crawling, and easy to extend.

from fastapi import FastAPI, Query
from utils.utils import *


# -------------------- Global (simple) cache --------------------
_page_cache: Dict[str, Page] = {}
_links_cache: List[str] = []
_links_initialized: bool = False


# -------------------- Scoring --------------------
_word_re = re.compile(r"[A-Za-z0-9']+")

def tokenize(s: str) -> List[str]:
    return [w.lower() for w in _word_re.findall(s)]

def count_phrase(text: str, phrase: str) -> int:
    # simple, case-insensitive substring count for the full phrase
    return text.lower().count(phrase.lower())

def count_terms(text: str, terms: List[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    low = text.lower()
    for t in terms:
        if not t:
            continue
        counts[t] = low.count(t.lower())
    return counts

def make_snippet(text: str, query: str, max_len: int = 240) -> Optional[str]:
    low = text.lower()
    q = query.lower()
    idx = low.find(q)
    if idx == -1:
        # try first query term
        parts = [p for p in tokenize(query) if len(p) > 2]
        for p in parts:
            idx = low.find(p)
            if idx != -1:
                break
    if idx == -1:
        return None
    start = max(0, idx - max_len // 3)
    end = min(len(text), idx + len(query) + max_len // 3)
    snippet = text[start:end].replace("\n", " ")
    return snippet + ("…" if end < len(text) else "")

# -------------------- FastAPI --------------------
app = FastAPI(title="SCP Frequency Search", version="0.1")

@app.get("/health")
async def health():
    return {"ok": True, "pages_cached": len(_page_cache), "links_cached": len(_links_cache)}

@app.post("/refresh")
async def refresh_index(limit: int = Query(200, ge=1, le=3000)):
    """(Re)build the in-memory index by crawling series pages and fetching up to `limit` SCP articles."""
    links = await build_seed_links(_links_cache, _links_initialized)
    await crawl_pages(links[:limit], _page_cache)
    return {"ok": True, "seed_links": len(links), "pages_cached": len(_page_cache)}

@app.get("/search", response_model=List[SearchResult])
async def search(query: str = Query(..., min_length=2), limit: int = Query(25, ge=1, le=100)):
    """
    Rank cached SCP pages by frequency of the query in the article text.
    - Score = 3 * phrase_hits + sum(term_hits)
    - If cache is empty, we'll bootstrap by crawling a small batch.
    """
    if not _links_initialized:
        # lazily build links and fetch a small initial set
        links = await build_seed_links(_links_cache, _links_initialized)
        await crawl_pages(links[:250], _page_cache)

    q_terms = [t for t in tokenize(query) if len(t) > 2]
    results: List[SearchResult] = []

    for page in _page_cache.values():
        phrase_hits = count_phrase(page.text, query)
        term_hits = count_terms(page.text, q_terms)
        raw_score = 3 * phrase_hits + sum(term_hits.values())
        if raw_score <= 0:
            continue
        results.append(
            SearchResult(
                url=page.url,
                title=page.title,
                score=int(raw_score),
                phrase_hits=int(phrase_hits),
                term_hits={k: int(v) for k, v in term_hits.items()},
                snippet=make_snippet(page.text, query),
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:limit]

# -------------------- Dev helper --------------------
# Run with: uvicorn scpsearchtool:app --reload --port 8000
# Example query: http://127.0.0.1:8000/search?query=Scarlet%20King%20SCPs
