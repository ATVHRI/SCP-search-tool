# scp_freq_search.py
# A minimal FastAPI service that crawls the SCP Wiki and ranks pages by how often a query appears in the article text.
# Focus: simple term/phrase frequency (not tags), polite crawling, and easy to extend.

import asyncio
import re
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from pydantic import BaseModel

# -------------------- Config --------------------
USER_AGENT = (
    "Mozilla/5.0 (compatible; scp-freq-bot/0.1; +https://example.com/bot)"
)
TIMEOUT = 20.0
CONCURRENCY = 8
REQUEST_DELAY_SECONDS = 0.8  # be gentle to the site

# Series index pages (SCP items). Add more as needed.
SERIES_INDEX_URLS = [
    "https://scp-wiki.wikidot.com/scp-series",
    "https://scp-wiki.wikidot.com/scp-series-2",
    "https://scp-wiki.wikidot.com/scp-series-3",
    # You can extend to -4, -5, -6, -7, etc., but start small while testing.
]

# Optional: tale hubs or other indices—commented out by default to keep demo smaller
# TALES_INDEX_URLS = [
#     "https://scp-wiki.wikidot.com/tales-by-title",
# ]

# -------------------- Models --------------------
@dataclass
class Page:
    url: str
    title: str
    text: str

class SearchResult(BaseModel):
    url: str
    title: str
    score: int
    phrase_hits: int
    term_hits: Dict[str, int]
    snippet: Optional[str]

# -------------------- Global (simple) cache --------------------
_page_cache: Dict[str, Page] = {}
_links_cache: List[str] = []
_links_initialized: bool = False

# -------------------- Utilities --------------------
async def fetch(client: httpx.AsyncClient, url: str) -> Optional[str]:
    """Fetch a URL and return text, with simple politeness delay."""
    try:
        r = await client.get(url, timeout=TIMEOUT)
        await asyncio.sleep(REQUEST_DELAY_SECONDS)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception:
        return None
    return None

def extract_main_text(html: str) -> Tuple[str, str]:
    """Return (title, main_text) from a wikidot page."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.find("title").get_text(strip=True) if soup.find("title") else ""
    main = soup.find(id="page-content") or soup.find("div", {"class": "content"})
    text = main.get_text("\n", strip=True) if main else soup.get_text("\n", strip=True)
    return title, text

_SCP_URL_PATTERN = re.compile(r"^https://scp-wiki\.wikidot\.com/scp-\d{3,4}$")

def extract_links_from_series(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("/"):
            href = "https://scp-wiki.wikidot.com" + href
        if _SCP_URL_PATTERN.match(href):
            links.append(href)
    return sorted(set(links))

async def build_seed_links() -> List[str]:
    global _links_cache, _links_initialized
    if _links_initialized and _links_cache:
        return _links_cache

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        series_htmls = await asyncio.gather(*[fetch(client, u) for u in SERIES_INDEX_URLS])

    links: List[str] = []
    for h in series_htmls:
        if not h:
            continue
        links.extend(extract_links_from_series(h))

    # You can extend with tales indexes here if desired.
    _links_cache = sorted(set(links))
    _links_initialized = True
    return _links_cache

async def crawl_pages(urls: List[str]) -> None:
    """Fetch and cache pages for given URLs (skip ones we already have)."""
    to_get = [u for u in urls if u not in _page_cache]
    if not to_get:
        return

    sem = asyncio.Semaphore(CONCURRENCY)

    async def _task(u: str):
        async with sem:
            async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
                html = await fetch(client, u)
            if not html:
                return
            title, text = extract_main_text(html)
            _page_cache[u] = Page(url=u, title=title, text=text)

    await asyncio.gather(*[_task(u) for u in to_get])

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
    links = await build_seed_links()
    await crawl_pages(links[:limit])
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
        links = await build_seed_links()
        await crawl_pages(links[:250])

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
# Run with: uvicorn scp_freq_search:app --reload --port 8000
# Example query: http://127.0.0.1:8000/search?query=Scarlet%20King%20SCPs
