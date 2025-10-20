import re
import asyncio
import httpx
from bs4 import BeautifulSoup

from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
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

async def build_seed_links(links_cache, links_init) -> List[str]:
    if links_init and links_cache:
        return links_cache

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        series_htmls = await asyncio.gather(*[fetch(client, u) for u in SERIES_INDEX_URLS])

    links: List[str] = []
    for h in series_htmls:
        if not h:
            continue
        links.extend(extract_links_from_series(h))

    # You can extend with tales indexes here if desired.
    # There's a good chance I broke this part. Not sure.
    links_cache = sorted(set(links))
    links_initialized = True
    return links_cache

async def crawl_pages(urls: List[str], page_cache) -> None:
    """Fetch and cache pages for given URLs (skip ones we already have)."""
    to_get = [u for u in urls if u not in page_cache]
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
            page_cache[u] = Page(url=u, title=title, text=text)

    await asyncio.gather(*[_task(u) for u in to_get])