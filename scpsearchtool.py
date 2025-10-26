# scp_freq_search.py
# A minimal FastAPI service that crawls the SCP Wiki and ranks pages by how often a query appears in the article text.
# Focus: simple term/phrase frequency (not tags), polite crawling, and easy to extend.
import psycopg2
from dotenv import load_dotenv
import os
import uuid
import pathlib
import httpx
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles


# Load environment variables from .env
load_dotenv()
import os
from supabase import create_client, Client
from dotenv import load_dotenv

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
SUPABASE_ANON_KEY = os.getenv('SUPABASE_ANON_KEY')
SUPABASE_JWT_SECRET = os.getenv('SUPABASE_JWT_SECRET')
SUPABASE_BUCKET = os.getenv('SUPABASE_BUCKET')

client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)




load_dotenv(dotenv_path='.env')
from fastapi import FastAPI, Query
from utils.utils import *
# ---------- Simple static dir for audio ----------
from pathlib import Path
BASE_DIR = Path(__file__).parent.resolve()
STATIC_DIR = BASE_DIR / "static"
AUDIO_DIR = STATIC_DIR / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# (Re)mount static safely
try:
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
except Exception:
    pass


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
@app.get("/", response_class=HTMLResponse)
def ui_home():
    return """
<!doctype html>
<html><head><meta charset="utf-8"/><title>SCP Search</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem}
.container{max-width:920px;margin:0 auto}
input[type=text]{width:360px;max-width:80vw;padding:.6rem}
button{padding:.6rem 1rem}
.result{border:1px solid #ddd;border-radius:10px;padding:12px;margin:10px 0}
.small{color:#666;font-size:.9rem}
a.button{display:inline-block;padding:.5rem .8rem;border-radius:8px;background:#111;color:#fff;text-decoration:none}
a.button:hover{background:#333}
</style></head>
<body>
<div class="container">
  <h1>SCP Search + Reader</h1>
  <form method="get" action="/ui/search">
    <input type="text" name="q" placeholder="Search SCP titles/keywords" required/>
    <button type="submit">Search</button>
  </form>
  <p class="small">Tip: paste a full SCP URL to open it directly.</p>
</div>
</body></html>"""


@app.get("/ui/search", response_class=HTMLResponse)
async def ui_search(q: str, limit: int = 25):
    # If user pasted a URL, jump straight to article page
    if q.startswith("http://") or q.startswith("https://"):
        return HTMLResponse(f'<meta http-equiv="refresh" content="0; url=/ui/article?url={q}"/>')

    # Reuse your existing /search logic by calling the function directly
    # (Since your /search endpoint returns List[SearchResult], we can call the same code path.)
    results = await search(query=q, limit=limit)  # type: ignore

    items = []
    if not results:
        items.append("<p>No results (cache empty?). Try clicking /refresh or paste a direct URL.</p>")
    else:
        for r in results:
            items.append(f"""
            <div class="result">
              <h3><a href="/ui/article?url={r.url}">{_escape_html(r.title)}</a></h3>
              <div class="small">{_escape_html(r.url)}</div>
              <p>{_escape_html(r.snippet or "")}</p>
              <p><a class="button" href="/ui/article?url={r.url}">Open</a></p>
            </div>""")

    return HTMLResponse(f"""
<!doctype html><html><head><meta charset="utf-8"/><title>Results: { _escape_html(q) }</title></head>
<body>
<div class="container">
  <p><a href="/">← New search</a></p>
  <h2>Results for: { _escape_html(q) }</h2>
  {''.join(items)}
</div>
</body></html>""")


@app.get("/ui/article", response_class=HTMLResponse)
async def ui_article(url: str):
    # Use cache when possible; otherwise fetch on demand
    page = _page_cache.get(url)
    if page is None:
        try:
            html = await _fetch_html(url)
        except Exception as e:
            return HTMLResponse(f"<p>Failed to fetch: {e}</p>", status_code=502)
        body_text = _extract_scp_body_from_html(html)
        title = url
    else:
        # Your crawler already stores clean text in page.text; use it
        body_text = page.text
        title = page.title or url

    if not body_text:
        return HTMLResponse("<p>Could not extract article body.</p>", status_code=404)

    tts_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
    return HTMLResponse(f"""
<!doctype html><html><head><meta charset="utf-8"/>
<title>{_escape_html(title)}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem}}
.container{{max-width:920px;margin:0 auto}}
pre{{white-space:pre-wrap}}
a.button{{display:inline-block;padding:.5rem .8rem;border-radius:8px;background:#111;color:#fff;text-decoration:none}}
a.button:hover{{background:#333}}
.small{{color:#666}}
</style></head>
<body>
<div class="container">
  <p><a href="/ui/search?q={_escape_html(url)}">← Back</a></p>
  <p><a class="button" href="/tts?url={_escape_html(url)}">Generate TTS</a> <span class="small">Then press play:</span></p>
  <audio controls style="width:100%" src="/audio?id={tts_id}"></audio>
  <hr/>
  <h2>{_escape_html(title)}</h2>
  <pre>{_escape_html(body_text)}</pre>
</div>
</body></html>""")


async def _fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SCPReader/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text


def _extract_scp_body_from_html(html: str) -> str:
    # Light-weight, no external parser required if you prefer (but BeautifulSoup is nicer).
    # Since you already depend on BeautifulSoup in your utils (likely), we’ll use it here for robustness:
    try:
        soup = BeautifulSoup(html, "html.parser")
        container = soup.select_one("div#page-content")
        if not container:
            # fallback to common names
            container = soup.select_one("div.content, div#content, article, div.article, div#main-content")
        if not container:
            return _clean_text(soup.get_text("\n"))

        from bs4 import BeautifulSoup
    except ImportError:
        # fallback: extremely rough cut
        import re
        m = re.search(r'<div id="page-content".*?>(.*?)</div>', html, re.I | re.S)
        if not m:
            # last resort: strip tags naively
            return _clean_text(_strip_tags(html))
        return _clean_text(_strip_tags(m.group(1)))


    # remove frequent junk on SCP + Wikidot
    for selector in [
        ".page-rate-widget-box", "#page-options-container", "#page-info", ".page-tags",
        ".footer-wikiwalk-nav", ".licensebox", "#discuss", ".options", ".breadcrumbs",
        ".mobile-top-bar", ".printuser"
    ]:
        for el in container.select(selector):
            el.decompose()

    return _clean_text(container.get_text("\n"))


def _strip_tags(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s)

def _clean_text(text: str) -> str:
    import re
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()
@app.get("/debug/audio", response_class=HTMLResponse)
def debug_audio():
    files = sorted(AUDIO_DIR.glob("*"))
    if not files:
        return HTMLResponse("<p>No audio files found in static/audio</p>")
    lis = "".join(f"<li>{f.name} — {f.stat().st_size} bytes</li>" for f in files)
    return HTMLResponse(f"<h3>static/audio</h3><ul>{lis}</ul>")


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

def _synthesize_tts_to_file(text: str, out_path: pathlib.Path) -> pathlib.Path:
    """
    Offline default using pyttsx3 (simple + no API keys).
    Swap in edge-tts / Coqui / etc. later if you want better voices.
    """
    try:
        import pyttsx3
    except ImportError as e:
        raise RuntimeError("pyttsx3 not installed. Run: pip install pyttsx3")

    out_path = out_path.with_suffix(".wav")
    engine = pyttsx3.init()
    # Optional tweaks:
    # engine.setProperty('rate', 180)
    # engine.setProperty('volume', 0.9)
    engine.save_to_file(text, str(out_path))
    engine.runAndWait()
    return out_path

def _tts_id_for_url(url: str) -> str:
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, url))
async def _get_body_text_for_url(url: str) -> str:
    page = _page_cache.get(url)
    if page and page.text:
        return page.text
    html = await _fetch_html(url)
    return _extract_scp_body_from_html(html)


@app.get("/audio")
async def get_audio(id: str = None, url: str = None):
    # Prefer explicit id
    if id:
        wav = AUDIO_DIR / f"{id}.wav"
        if wav.exists():
            return FileResponse(wav, media_type="audio/wav", filename=f"{id}.wav")
        # If file missing but we have a URL, lazy-generate
        if url:
            tts_id = _tts_id_for_url(url)
            if tts_id != id:
                return PlainTextResponse("id/url mismatch", status_code=400)
            # Generate then serve
            text = await _get_body_text_for_url(url)
            if not text:
                return PlainTextResponse("Could not extract article body", status_code=404)
            out = AUDIO_DIR / tts_id
            try:
                _synthesize_tts_to_file(text, out)
            except Exception as e:
                return PlainTextResponse(f"TTS failed: {e}", status_code=500)
            return FileResponse(out.with_suffix(".wav"), media_type="audio/wav", filename=f"{tts_id}.wav")
        # No url provided → ask to hit /tts first
        return PlainTextResponse("Audio not found. Generate it via /tts or provide ?url=", status_code=404)

    # If only URL is provided (no id), compute id and recurse
    if url:
        tts_id = _tts_id_for_url(url)
        return await get_audio(id=tts_id, url=url)

    return PlainTextResponse("Provide ?id= or ?url=", status_code=400)



@app.get("/tts")
async def tts(url: str):
    # Prefer cache text if available; otherwise fetch + extract
    page = _page_cache.get(url)
    if page is not None and page.text:
        body_text = page.text
    else:
        html = await _fetch_html(url)
        body_text = _extract_scp_body_from_html(html)

    if not body_text:
        return PlainTextResponse("Could not extract article body", status_code=404)

    tts_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
    out = AUDIO_DIR / tts_id
    try:
        audio_path = _synthesize_tts_to_file(body_text, out)
    except Exception as e:
        return PlainTextResponse(f"TTS failed: {e}", status_code=500)

    < audio
    controls
    style = "width:100%"
    src = "/audio?id={{tts_id}}&url={{url_encoded}}" > < / audio >

    return PlainTextResponse(f"/audio?id={tts_id}")

def _synthesize_tts_to_file(text: str, out_path: Path) -> Path:
    import pyttsx3, time
    out_path = out_path.with_suffix(".wav")
    eng = pyttsx3.init()
    # eng.setProperty("rate", 180)  # optional
    eng.save_to_file(text, str(out_path))
    eng.runAndWait()
    # Sanity check
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"TTS did not produce file: {out_path}")
    print(f"[TTS] Wrote {out_path} ({out_path.stat().st_size} bytes) at {time.strftime('%X')}")
    return out_path

@app.get("/audio")
def audio(id: str):
    wav = AUDIO_DIR / f"{id}.wav"
    if wav.exists():
        return FileResponse(wav, media_type="audio/wav", filename=f"{id}.wav")
    return PlainTextResponse("Audio not found. Hit /tts first.", status_code=404)

def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


# -------------------- Dev helper --------------------
# Run with: uvicorn scpsearchtool:app --reload --port 8000
# Example query: http://127.0.0.1:8000/search?query=Scarlet%20King%20SCPs
