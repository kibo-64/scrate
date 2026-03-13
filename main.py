"""
Scrawl API â Backend
Universal review aggregator: movies, TV, games, music, books, products
OmniScore = weighted AI-calculated master score across all sources
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import json
import os
import re
import asyncio
import hashlib
from datetime import datetime, timedelta
import unicodedata
from bs4 import BeautifulSoup

# âââ CONFIG ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

TMDB_API_KEY          = os.getenv("TMDB_API_KEY", "")
RAWG_API_KEY          = os.getenv("RAWG_API_KEY", "")
ANTHROPIC_API_KEY     = os.getenv("ANTHROPIC_API_KEY", "")
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN  = os.getenv("CLOUDFLARE_API_TOKEN", "")

# How long to keep cached results before refreshing (hours)
CACHE_TTL_HOURS = 72

# In-memory cache: {hash: {data, expires_at}}
_cache: dict = {}

# âââ APP âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

app = FastAPI(title="Scrawl API", version="1.3.0", description="scrawl it before you fall for it")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten to your domain once live
    allow_methods=["*"],
    allow_headers=["*"],
)

# âââ CACHE HELPERS âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def make_key(query: str) -> str:
    return hashlib.md5(query.lower().strip().encode()).hexdigest()

def cache_get(key: str):
    entry = _cache.get(key)
    if not entry:
        return None
    if datetime.now() > entry["expires_at"]:
        del _cache[key]
        return None
    return entry["data"]

def cache_set(key: str, data: dict):
    _cache[key] = {
        "data": data,
        "expires_at": datetime.now() + timedelta(hours=CACHE_TTL_HOURS),
    }

# âââ SCORE HELPERS âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def normalize_to_100(score_str: str, out_of: str = None) -> float | None:
    """Convert any score format (4.5/5, 8.8/10, 87/100, 94%) â 0-100 float."""
    try:
        val = float(re.sub(r"[^0-9.]", "", score_str))
    except (ValueError, TypeError):
        return None

    if out_of:
        try:
            max_val = float(out_of)
            if max_val > 0:
                return round((val / max_val) * 100, 1)
        except ValueError:
            pass

    # Auto-detect scale
    if val <= 5:
        return round((val / 5) * 100, 1)
    elif val <= 10:
        return round((val / 10) * 100, 1)
    else:
        return round(min(val, 100), 1)


def calculate_omniscore(sources: list) -> int:
    """
    Weighted average of all source sentiments.
    Expert reviews carry 1.5x weight, Users 1.0x, Community 0.8x.
    """
    if not sources:
        return 0

    weight_map = {"Expert": 1.5, "Users": 1.0, "Community": 0.8}
    weighted_sum = 0.0
    total_weight = 0.0

    for s in sources:
        sentiment = s.get("sentiment")
        if sentiment is None:
            continue
        w = weight_map.get(s.get("type", "Users"), 1.0)
        weighted_sum += sentiment * w
        total_weight += w

    if total_weight == 0:
        return 0

    raw = weighted_sum / total_weight
    return min(100, max(0, round(raw)))


def verdict_from_score(score: int) -> str:
    if score >= 90:  return "Exceptional"
    if score >= 80:  return "Highly Recommended"
    if score >= 70:  return "Solid Choice"
    if score >= 55:  return "Mixed Reviews"
    return "Proceed with Caution"


def build_source(name, source_type, icon, color, score_str, out_of, reviews=None, is_award=False):
    """Build a standardised source dict including auto-computed sentiment."""
    sentiment = normalize_to_100(score_str, out_of) if not is_award else 95
    return {
        "name":      name,
        "type":      source_type,
        "icon":      icon,
        "color":     color,
        "score":     score_str,
        "outOf":     out_of,
        "reviews":   reviews,
        "sentiment": int(sentiment) if sentiment is not None else 75,
        "isAward":   is_award,
    }

# âââ HTTP HEADERS âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# âââ CATEGORY DETECTION âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

GAME_KW    = {"game","ps5","xbox","nintendo","steam","playstation","pokemon",
               "zelda","mario","call of duty","fifa","fortnite","minecraft","elden ring"}
MUSIC_KW   = {"album","song","artist","band","track","ep","lp","mixtape","single","discography"}
BOOK_KW    = {"book","novel","author","memoir","biography","fiction","nonfiction","trilogy"}
MOVIE_KW   = {"movie","film","cinema","director","sequel","prequel","box office","series","show","season"}
CAR_KW     = {"car","suv","truck","sedan","coupe","hatchback","convertible","minivan","pickup",
               "horsepower","mpg","torque","0-60","turbo","hybrid","electric vehicle",
               "toyota","honda","ford","chevrolet","bmw","mercedes","audi","tesla",
               "hyundai","kia","nissan","volkswagen","subaru","mazda","porsche","lexus"}

def detect_category(query: str) -> str:
    q = query.lower()
    for kw in GAME_KW:
        if kw in q: return "game"
    for kw in MUSIC_KW:
        if kw in q: return "music"
    for kw in BOOK_KW:
        if kw in q: return "book"
    for kw in MOVIE_KW:
        if kw in q: return "movie"
    for kw in CAR_KW:
        if kw in q: return "car"
    return "auto"

# âââ SCRAPERS ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def scrape_metacritic_movie(title: str, year: str, client: httpx.AsyncClient) -> dict | None:
    """Attempt to scrape Metacritic score for a movie/show."""
    slug = re.sub(r"[^a-z0-9\s-]", "", title.lower()).strip().replace(" ", "-")
    urls_to_try = [
        f"https://www.metacritic.com/movie/{slug}/",
        f"https://www.metacritic.com/tv/{slug}/",
    ]
    for url in urls_to_try:
        try:
            r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            # Metacritic embeds score in JSON-LD
            ld = soup.find("script", type="application/ld+json")
            if ld:
                data = json.loads(ld.string)
                rating = data.get("aggregateRating", {})
                val = rating.get("ratingValue")
                if val:
                    return build_source(
                        "Metacritic", "Expert", "&#128202;", "#ffcc34",
                        str(int(float(val))), "100"
                    )
        except Exception:
            continue
    return None


async def scrape_rt_movie(title: str, client: httpx.AsyncClient) -> dict | None:
    """Attempt to scrape Rotten Tomatoes Tomatometer."""
    slug = re.sub(r"[^a-z0-9\s_]", "", title.lower()).strip().replace(" ", "_")
    url = f"https://www.rottentomatoes.com/m/{slug}"
    try:
        r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        # RT embeds score as JSON in a script tag
        for script in soup.find_all("script"):
            text = script.string or ""
            if "tomatoMeter" in text or "criticsScore" in text:
                m = re.search(r'"tomatoMeter":\s*(\d+)', text)
                if not m:
                    m = re.search(r'"criticsScore":\s*(\d+)', text)
                if m:
                    score = m.group(1)
                    return build_source(
                        "Rotten Tomatoes", "Expert", "&#127813;", "#fa320a",
                        score + "%", None
                    )
    except Exception:
        pass
    return None


async def scrape_imdb(title: str, year: str, client: httpx.AsyncClient) -> dict | None:
    """Try to pull IMDb rating from their search."""
    try:
        url = f"https://www.imdb.com/find/?q={title.replace(' ', '+')}&s=tt&ttype=ft"
        r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        # Find first result link
        result = soup.find("a", href=re.compile(r"/title/tt\d+/"))
        if not result:
            return None
        href = result["href"]
        m = re.search(r"/title/(tt\d+)/", href)
        if not m:
            return None
        tt_id = m.group(1)
        # Fetch that title page
        title_url = f"https://www.imdb.com/title/{tt_id}/"
        tr = await client.get(title_url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
        if tr.status_code != 200:
            return None
        tsoup = BeautifulSoup(tr.text, "html.parser")
        ld = tsoup.find("script", type="application/ld+json")
        if ld:
            data = json.loads(ld.string)
            rating = data.get("aggregateRating", {})
            val = rating.get("ratingValue")
            count = rating.get("ratingCount")
            if val:
                return build_source(
                    "IMDb", "Users", "&#11088;", "#f5c518",
                    str(val), "10",
                    reviews=f"{int(count):,}" if count else None
                )
    except Exception:
        pass
    return None

# âââ CANDIDATE HELPERS (lightweight, no scraping) ââââââââââââââââââââââââââââ

async def candidates_rawg(query: str, client: httpx.AsyncClient) -> list:
    if not RAWG_API_KEY:
        return []
    try:
        r = await client.get(
            "https://api.rawg.io/api/games",
            params={
                "key": RAWG_API_KEY,
                "search": query,
                "page_size": 10,
            },
        )
        results = []
        for g in r.json().get("results", [])[:10]:
            genres = [gen["name"] for gen in g.get("genres", [])[:2]]
            raw_rating = g.get("rating", 0)
            results.append({
                "id":         str(g["id"]),
                "category":   "game",
                "title":      g.get("name", ""),
                "year":       (g.get("released") or "")[:4],
                "image_url":  g.get("background_image"),
                "subtitle":   ", ".join(genres),
                "rating":     round(raw_rating, 1) if raw_rating else None,
                "rating_max": 5,
            })
        return results
    except Exception:
        return []


async def candidates_tmdb(query: str, client: httpx.AsyncClient) -> list:
    if not TMDB_API_KEY:
        return []
    try:
        r = await client.get(
            "https://api.themoviedb.org/3/search/multi",
            params={"api_key": TMDB_API_KEY, "query": query, "language": "en-US", "page": 1},
        )
        results = []
        for item in r.json().get("results", []):
            if item.get("media_type") not in ("movie", "tv"):
                continue
            title  = item.get("title") or item.get("name", "")
            year   = (item.get("release_date") or item.get("first_air_date", ""))[:4]
            poster = (
                f"https://image.tmdb.org/t/p/w200{item['poster_path']}"
                if item.get("poster_path") else None
            )
            vote_avg = item.get("vote_average", 0)
            results.append({
                "id":         str(item["id"]),
                "category":   item["media_type"],
                "title":      title,
                "year":       year,
                "image_url":  poster,
                "subtitle":   (item.get("overview") or "")[:120],
                "rating":     round(vote_avg, 1) if vote_avg else None,
                "rating_max": 10,
            })
            if len(results) >= 8:
                break
        return results
    except Exception:
        return []


async def candidates_books(query: str, client: httpx.AsyncClient) -> list:
    try:
        r = await client.get(
            "https://openlibrary.org/search.json",
            params={
                "q": query, "limit": 8,
                "fields": "key,title,author_name,first_publish_year,cover_i",
            },
        )
        results = []
        for book in r.json().get("docs", [])[:8]:
            cover_id  = book.get("cover_i")
            image_url = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else None
            authors   = book.get("author_name", [])
            # key looks like "/works/OL45883W" â store without leading slash
            raw_key   = book.get("key", "")
            book_id   = raw_key.lstrip("/")
            results.append({
                "id":        book_id,
                "category":  "book",
                "title":     book.get("title", ""),
                "year":      str(book.get("first_publish_year", "")),
                "image_url": image_url,
                "subtitle":  ", ".join(authors[:2]) if authors else "",
            })
        return results
    except Exception:
        return []


async def candidates_music(query: str, client: httpx.AsyncClient) -> list:
    mb_headers = {
        "User-Agent": "Scrawl/1.0 (scrawl.live; contact@scrawl.live)",
        "Accept": "application/json",
    }
    try:
        r = await client.get(
            "https://musicbrainz.org/ws/2/release/",
            params={"query": query, "fmt": "json", "limit": 8},
            headers=mb_headers,
        )
        results = []
        for rel in r.json().get("releases", [])[:8]:
            artist = ""
            credits = rel.get("artist-credit", [])
            if credits:
                artist = credits[0].get("name", "")
            results.append({
                "id":        rel.get("id", ""),
                "category":  "music",
                "title":     rel.get("title", ""),
                "year":      (rel.get("date") or "")[:4],
                "image_url": None,  # skip cover art for speed
                "subtitle":  artist,
            })
        return results
    except Exception:
        return []

# âââ CLOUDFLARE BROWSER RENDERING ââââââââââââââââââââââââââââââââââââââââââââââ

async def cf_fetch(url: str, client: httpx.AsyncClient) -> str | None:
    """Fetch a JS-rendered page via Cloudflare Browser Rendering /content API."""
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        return None
    try:
        r = await client.post(
            f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/browser-rendering/content",
            headers={
                "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "url": url,
                "rejectResourceTypes": ["image", "stylesheet", "font", "media"],
            },
            timeout=35,
        )
        if r.status_code == 200:
            # API may return JSON-wrapped or raw HTML
            ct = r.headers.get("content-type", "")
            if "json" in ct:
                data = r.json()
                return (
                    data.get("result", {}).get("content")
                    or data.get("result")
                    or data.get("content")
                    or r.text
                )
            return r.text
    except Exception:
        pass
    return None


# âââ NEW SCRAPERS (v1.3.0) ââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def score_opencritic(title: str, client: httpx.AsyncClient) -> dict | None:
    """OpenCritic public API â no key required."""
    try:
        sr = await client.get(
            "https://api.opencritic.com/api/game/search",
            params={"criteria": title},
            headers={"User-Agent": "Scrawl/1.3"},
            timeout=10,
        )
        results = sr.json()
        if not results:
            return None
        game_id = results[0].get("id")
        if not game_id:
            return None
        dr = await client.get(
            f"https://api.opencritic.com/api/game/{game_id}",
            headers={"User-Agent": "Scrawl/1.3"},
            timeout=10,
        )
        detail = dr.json()
        score       = detail.get("topCriticScore", -1)
        num_reviews = detail.get("numReviews", 0)
        if score is None or score < 0:
            return None
        return build_source(
            "OpenCritic", "Expert", "&#128270;", "#7b1fa2",
            str(round(score)), "100",
            reviews=f"{num_reviews}" if num_reviews else None,
        )
    except Exception:
        return None


async def score_steam(rawg_id: str, client: httpx.AsyncClient) -> dict | None:
    """Steam user review score, fetched via RAWG stores endpoint then Steam API."""
    if not RAWG_API_KEY:
        return None
    try:
        sr = await client.get(
            f"https://api.rawg.io/api/games/{rawg_id}/stores",
            params={"key": RAWG_API_KEY},
            timeout=10,
        )
        steam_url = None
        for store in sr.json().get("results", []):
            u = store.get("url", "")
            if "store.steampowered.com" in u:
                steam_url = u
                break
        if not steam_url:
            return None
        m = re.search(r"/app/(\d+)", steam_url)
        if not m:
            return None
        app_id = m.group(1)
        rr = await client.get(
            f"https://store.steampowered.com/appreviews/{app_id}",
            params={"json": "1", "language": "all", "num_per_page": "0"},
            headers=BROWSER_HEADERS,
            timeout=10,
        )
        qs       = rr.json().get("query_summary", {})
        total    = qs.get("total_reviews", 0)
        positive = qs.get("total_positive", 0)
        if total == 0:
            return None
        score = round((positive / total) * 100)
        return build_source(
            "Steam", "Users", "&#127918;", "#1b2838",
            f"{score}%", None,
            reviews=f"{total:,}",
        )
    except Exception:
        return None


async def score_rogerebert(title: str, year: str, client: httpx.AsyncClient) -> dict | None:
    """Roger Ebert / RogerEbert.com â JSON-LD star rating (0â4)."""
    try:
        slug = re.sub(r"[^a-z0-9\s-]", "", title.lower()).strip().replace(" ", "-")
        urls = [f"https://www.rogerebert.com/reviews/{slug}-{year}" if year else None,
                f"https://www.rogerebert.com/reviews/{slug}"]
        for url in urls:
            if not url:
                continue
            try:
                r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                for ld_tag in soup.find_all("script", type="application/ld+json"):
                    try:
                        ld  = json.loads(ld_tag.string)
                        rat = ld.get("reviewRating") or ld.get("aggregateRating") or {}
                        val  = rat.get("ratingValue")
                        best = rat.get("bestRating", 4)
                        if val is not None:
                            return build_source(
                                "Roger Ebert", "Expert", "&#127902;", "#cc0000",
                                str(val), str(best),
                            )
                    except Exception:
                        continue
            except Exception:
                continue
        return None
    except Exception:
        return None


async def score_letterboxd(title: str, year: str, client: httpx.AsyncClient) -> dict | None:
    """Letterboxd average user rating (0â5 stars)."""
    try:
        slug = re.sub(r"[^a-z0-9\s-]", "", title.lower()).strip().replace(" ", "-")
        url  = f"https://letterboxd.com/film/{slug}/"
        html = await cf_fetch(url, client)
        if not html:
            r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
            if r.status_code != 200:
                return None
            html = r.text
        soup = BeautifulSoup(html, "html.parser")
        # Prefer JSON-LD aggregateRating
        for ld_tag in soup.find_all("script", type="application/ld+json"):
            try:
                ld    = json.loads(ld_tag.string)
                rat   = ld.get("aggregateRating", {})
                val   = rat.get("ratingValue")
                count = rat.get("ratingCount")
                if val:
                    return build_source(
                        "Letterboxd", "Users", "&#127909;", "#00c030",
                        str(val), "5",
                        reviews=f"{int(count):,}" if count else None,
                    )
            except Exception:
                continue
        # Fallback: twitter card meta
        meta = soup.find("meta", attrs={"name": "twitter:data2"})
        if meta:
            m = re.search(r"([\d.]+)\s+out of\s+([\d.]+)", meta.get("content", ""))
            if m:
                return build_source(
                    "Letterboxd", "Users", "&#127909;", "#00c030",
                    m.group(1), m.group(2),
                )
        return None
    except Exception:
        return None


async def score_pitchfork(title: str, artist: str, client: httpx.AsyncClient) -> dict | None:
    """Pitchfork album review score (0â10)."""
    try:
        query = f"{title} {artist}".strip().replace(" ", "+")
        search_url = f"https://pitchfork.com/search/?query={query}&types=reviews"
        html = await cf_fetch(search_url, client)
        if not html:
            r = await client.get(search_url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
            if r.status_code != 200:
                return None
            html = r.text
        soup = BeautifulSoup(html, "html.parser")
        link = soup.find("a", href=re.compile(r"/reviews/albums/"))
        if not link:
            return None
        review_url = "https://pitchfork.com" + link["href"]
        html2 = await cf_fetch(review_url, client)
        if not html2:
            r2 = await client.get(review_url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
            if r2.status_code != 200:
                return None
            html2 = r2.text
        soup2 = BeautifulSoup(html2, "html.parser")
        # Score lives in a <p> or <span> with class containing "rating" or "score"
        for cls in [re.compile(r"Rating-"), re.compile(r"score"), re.compile(r"rating")]:
            el = soup2.find(class_=cls)
            if el:
                m = re.search(r"(\d+\.?\d*)", el.get_text())
                if m:
                    return build_source(
                        "Pitchfork", "Expert", "&#127930;", "#ee3322",
                        m.group(1), "10",
                    )
        # JSON-LD fallback
        for ld_tag in soup2.find_all("script", type="application/ld+json"):
            try:
                ld  = json.loads(ld_tag.string)
                rat = ld.get("reviewRating") or ld.get("aggregateRating") or {}
                val = rat.get("ratingValue")
                if val:
                    return build_source("Pitchfork", "Expert", "&#127930;", "#ee3322", str(val), "10")
            except Exception:
                continue
        return None
    except Exception:
        return None


async def score_allmusic(title: str, artist: str, client: httpx.AsyncClient) -> dict | None:
    """AllMusic editor rating (out of 10, displayed as stars)."""
    try:
        query = f"{title} {artist}".strip().replace(" ", "%20")
        search_url = f"https://www.allmusic.com/search/albums/{query}"
        html = await cf_fetch(search_url, client)
        if not html:
            r = await client.get(search_url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
            if r.status_code != 200:
                return None
            html = r.text
        soup = BeautifulSoup(html, "html.parser")
        link = soup.find("a", href=re.compile(r"/album/"))
        if not link:
            return None
        album_url = link["href"]
        if not album_url.startswith("http"):
            album_url = "https://www.allmusic.com" + album_url
        html2 = await cf_fetch(album_url, client)
        if not html2:
            r2 = await client.get(album_url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
            if r2.status_code != 200:
                return None
            html2 = r2.text
        soup2 = BeautifulSoup(html2, "html.parser")
        # AllMusic editor rating: div.allmusic-rating or span with aria-label like "4.5 out of 5"
        for el in soup2.find_all(True, attrs={"aria-label": re.compile(r"out of")}):
            m = re.search(r"([\d.]+)\s+out of\s+([\d.]+)", el.get("aria-label", ""))
            if m:
                return build_source(
                    "AllMusic", "Expert", "&#127925;", "#016BAE",
                    m.group(1), m.group(2),
                )
        # Fallback: star count from class name
        star_el = soup2.find(class_=re.compile(r"allmusic-rating|editor-rating"))
        if star_el:
            m = re.search(r"(\d+\.?\d*)", star_el.get_text())
            if m:
                return build_source("AllMusic", "Expert", "&#127925;", "#016BAE", m.group(1), "5")
        return None
    except Exception:
        return None


async def score_ign(title: str, category: str, client: httpx.AsyncClient) -> dict | None:
    """IGN review score (0-10).

    Strategy:
    1. Try direct IGN game/movie page (ign.com/games/{slug}) - parse JSON-LD,
       __NEXT_DATA__ embedded JSON, or ratingValue regex.
    2. Fall back to IGN search page -> follow review article link.
    """
    try:
        ign_type = "games" if category == "game" else "movies" if category in ("movie", "tv") else None
        if not ign_type:
            return None

        # Build slug from title
        slug = re.sub(r'[^\w\s-]', '', title.lower())
        slug = re.sub(r'[\s_]+', '-', slug)
        slug = re.sub(r'-+', '-', slug).strip('-')

        def _extract_score(html: str) -> dict | None:
            """Try every known extraction method on an IGN page."""
            # 1. JSON-LD
            soup = BeautifulSoup(html, "html.parser")
            for ld_tag in soup.find_all("script", type="application/ld+json"):
                try:
                    ld = json.loads(ld_tag.string or "")
                    rat = ld.get("reviewRating") or ld.get("aggregateRating")
                    if rat:
                        val = rat.get("ratingValue")
                        best = rat.get("bestRating", 10)
                        if val:
                            return build_source(
                                "IGN", "Expert", "&#128269;", "#ff5722",
                                str(val), str(best),
                            )
                except Exception:
                    continue

            # 2. __NEXT_DATA__ - targeted paths then BFS
            nd = soup.find("script", id="__NEXT_DATA__")
            if nd and nd.string:
                try:
                    data = json.loads(nd.string)

                    def _deep_get(obj, *keys):
                        for k in keys:
                            if isinstance(obj, dict):
                                obj = obj.get(k)
                            else:
                                return None
                        return obj

                    pp = _deep_get(data, "props", "pageProps")
                    for path in [
                        ("article", "contentScore", "score"),
                        ("article", "scoreCard", "score"),
                        ("review", "contentScore", "score"),
                        ("review", "scoreCard", "score"),
                        ("page", "contentScore", "score"),
                        ("content", "contentScore", "score"),
                    ]:
                        val = _deep_get(pp, *path) if pp else None
                        if val is not None:
                            try:
                                fval = float(str(val))
                                if 0 < fval <= 10:
                                    return build_source(
                                        "IGN", "Expert", "&#128269;", "#ff5722",
                                        str(fval), "10",
                                    )
                            except ValueError:
                                pass

                    # BFS fallback
                    stack = [data]
                    visited = 0
                    while stack and visited < 300:
                        node = stack.pop()
                        visited += 1
                        if isinstance(node, dict):
                            val = node.get("ratingValue")
                            if val:
                                try:
                                    fval = float(str(val))
                                    if 0 < fval <= 10:
                                        best = node.get("bestRating", 10)
                                        return build_source(
                                            "IGN", "Expert", "&#128269;", "#ff5722",
                                            str(fval), str(best),
                                        )
                                except ValueError:
                                    pass
                            stack.extend(node.values())
                        elif isinstance(node, list):
                            stack.extend(node)
                except Exception:
                    pass

            # 3. Regex fallback
            m = re.search(r'"ratingValue":\s*"?([\d.]+)"?', html)
            if m:
                return build_source("IGN", "Expert", "&#128269;", "#ff5722", m.group(1), "10")
            m2 = re.search(r'"score":\s*([\d.]+)\s*[,}]', html)
            if m2:
                try:
                    s = float(m2.group(1))
                    if 0 < s <= 10:
                        return build_source("IGN", "Expert", "&#128269;", "#ff5722", str(s), "10")
                except ValueError:
                    pass
            return None

        # Step 1: direct page URL
        direct_url = f"https://www.ign.com/{ign_type}/{slug}"
        html = await cf_fetch(direct_url, client)
        if not html:
            try:
                r = await client.get(
                    direct_url, headers=BROWSER_HEADERS,
                    follow_redirects=True, timeout=12,
                )
                if r.status_code == 200:
                    html = r.text
            except Exception:
                pass
        if html:
            result = _extract_score(html)
            if result:
                return result

        # Step 2: IGN search -> follow review link
        query = title.replace(" ", "+")
        search_url = f"https://www.ign.com/search?q={query}&type={ign_type}&filter=reviews"
        html = await cf_fetch(search_url, client)
        if not html:
            try:
                r = await client.get(
                    search_url, headers=BROWSER_HEADERS,
                    follow_redirects=True, timeout=12,
                )
                if r.status_code == 200:
                    html = r.text
            except Exception:
                pass
        if html:
            soup = BeautifulSoup(html, "html.parser")
            link = soup.find("a", href=re.compile(
                r"(ign\.com)/(articles|reviews)/|"
                r"ign\.com/(games|movies)/[^/]+/review"
            ))
            if link:
                review_url = link["href"]
                if not review_url.startswith("http"):
                    review_url = "https://www.ign.com" + review_url
                html2 = await cf_fetch(review_url, client)
                if not html2:
                    try:
                        r2 = await client.get(
                            review_url, headers=BROWSER_HEADERS,
                            follow_redirects=True, timeout=12,
                        )
                        if r2.status_code == 200:
                            html2 = r2.text
                    except Exception:
                        pass
                if html2:
                    result = _extract_score(html2)
                    if result:
                        return result

    except Exception:
        pass
    return None


async def score_gamespot(title: str, client: httpx.AsyncClient) -> dict | None:
    """GameSpot review score (0-10)."""
    try:
        slug = re.sub(r'[^\w\s-]', '', title.lower())
        slug = re.sub(r'[\s_]+', '-', slug)
        slug = re.sub(r'-+', '-', slug).strip('-')

        def _extract_gs(html: str) -> dict | None:
            soup = BeautifulSoup(html, "html.parser")
            for ld_tag in soup.find_all("script", type="application/ld+json"):
                try:
                    ld = json.loads(ld_tag.string or "")
                    items = ld if isinstance(ld, list) else [ld]
                    for item in items:
                        rat = item.get("reviewRating") or item.get("aggregateRating")
                        if rat:
                            val = rat.get("ratingValue")
                            best = rat.get("bestRating", 10)
                            if val:
                                return build_source(
                                    "GameSpot", "Expert", "&#127918;", "#FF6B00",
                                    str(val), str(best),
                                )
                except Exception:
                    continue
            m = re.search(r'"ratingValue":\s*"?([\d.]+)"?', html)
            if m:
                return build_source("GameSpot", "Expert", "&#127918;", "#FF6B00", m.group(1), "10")
            return None

        # Direct game page
        url = f"https://www.gamespot.com/games/{slug}/"
        html = await cf_fetch(url, client)
        if not html:
            try:
                r = await client.get(
                    url, headers=BROWSER_HEADERS,
                    follow_redirects=True, timeout=12,
                )
                if r.status_code == 200:
                    html = r.text
            except Exception:
                pass
        if html:
            result = _extract_gs(html)
            if result:
                return result

        # Search fallback
        query = title.replace(" ", "+")
        search_url = f"https://www.gamespot.com/search/?q={query}&type=reviews"
        html = await cf_fetch(search_url, client)
        if not html:
            try:
                r = await client.get(
                    search_url, headers=BROWSER_HEADERS,
                    follow_redirects=True, timeout=12,
                )
                if r.status_code == 200:
                    html = r.text
            except Exception:
                pass
        if html:
            soup = BeautifulSoup(html, "html.parser")
            link = soup.find("a", href=re.compile(r"gamespot\.com/(articles|reviews)/"))
            if link:
                review_url = link["href"]
                if not review_url.startswith("http"):
                    review_url = "https://www.gamespot.com" + review_url
                html2 = await cf_fetch(review_url, client)
                if not html2:
                    try:
                        r2 = await client.get(
                            review_url, headers=BROWSER_HEADERS,
                            follow_redirects=True, timeout=12,
                        )
                        if r2.status_code == 200:
                            html2 = r2.text
                    except Exception:
                        pass
                if html2:
                    return _extract_gs(html2)

    except Exception:
        pass
    return None


# ─── POLYGON SCRAPER ───────────────────────────────────────────────────────────
async def score_polygon(title: str, category: str, client: httpx.AsyncClient) -> dict | None:
    """Polygon review score. Tries direct slug URL then falls back to search."""
    try:
        slug = re.sub(r"[^a-z0-9\s-]", "", title.lower()).strip()
        slug = re.sub(r"[\s_]+", "-", slug).strip("-")
        if category == "game":
            urls = [f"https://www.polygon.com/reviews/game/{slug}",
                    f"https://www.polygon.com/game/{slug}"]
        elif category in ("movie", "tv"):
            urls = [f"https://www.polygon.com/reviews/{slug}",
                    f"https://www.polygon.com/movie/{slug}"]
        else:
            return None
        for url in urls:
            try:
                r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                # JSON-LD first
                for ld_tag in soup.find_all("script", type="application/ld+json"):
                    try:
                        ld = json.loads(ld_tag.string or "")
                        rat = ld.get("reviewRating") or ld.get("aggregateRating") or {}
                        val = rat.get("ratingValue")
                        best = rat.get("bestRating", 10)
                        if val:
                            return build_source("Polygon", "Expert", "&#128308;", "#ff4713",
                                                str(val), str(best))
                    except Exception:
                        continue
                # Regex fallback
                m = re.search(r'"ratingValue"\s*:\s*"?([\d.]+)"?', r.text)
                best_m = re.search(r'"bestRating"\s*:\s*"?([\d.]+)"?', r.text)
                if m:
                    return build_source("Polygon", "Expert", "&#128308;", "#ff4713",
                                        m.group(1), best_m.group(1) if best_m else "10")
            except Exception:
                continue
    except Exception:
        pass
    return None


# ─── CAR SCRAPERS ──────────────────────────────────────────────────────────────
async def score_nhtsa_safety(year: str, make: str, model: str, client: httpx.AsyncClient) -> dict | None:
    """NHTSA 5-star safety rating — two-step: get VehicleId then fetch overall rating."""
    try:
        if not year or not make or not model:
            return None
        # Step 1: find matching VehicleId(s)
        r1 = await client.get(
            f"https://api.nhtsa.gov/SafetyRatings/modelyear/{year}/make/{make}/model/{model}",
            timeout=8,
        )
        vehicles = r1.json().get("Results", [])
        if not vehicles:
            return None
        vehicle_id = vehicles[0].get("VehicleId")
        if not vehicle_id:
            return None
        # Step 2: fetch detailed ratings for that vehicle
        r2 = await client.get(
            f"https://api.nhtsa.gov/SafetyRatings/VehicleId/{vehicle_id}",
            timeout=8,
        )
        detail = (r2.json().get("Results") or [{}])[0]
        overall = detail.get("OverallRating")
        if not overall or overall == "Not Rated":
            return None
        return build_source("NHTSA Safety", "Expert", "&#128737;", "#002868",
                             str(overall), "5", reviews="Gov't crash tests")
    except Exception:
        return None


async def score_edmunds(year: str, make: str, model: str, client: httpx.AsyncClient) -> dict | None:
    """Edmunds expert rating (0-10)."""
    try:
        make_slug  = re.sub(r"[^a-z0-9]", "", make.lower())
        model_slug = re.sub(r"[^a-z0-9-]", "", model.lower().replace(" ", "-"))
        url = f"https://www.edmunds.com/{make_slug}/{model_slug}/{year}/review/"
        r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        # JSON-LD
        for ld_tag in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(ld_tag.string or "")
                rat = ld.get("reviewRating") or ld.get("aggregateRating") or {}
                val = rat.get("ratingValue")
                best = rat.get("bestRating", 10)
                if val:
                    return build_source("Edmunds", "Expert", "&#127775;", "#0f6ab4",
                                        str(val), str(best))
            except Exception:
                continue
        # Regex fallback
        m = re.search(r'"ratingValue"\s*:\s*"?([\d.]+)"?', r.text)
        if m:
            return build_source("Edmunds", "Expert", "&#127775;", "#0f6ab4", m.group(1), "10")
    except Exception:
        pass
    return None


async def score_caranddriver(year: str, make: str, model: str, client: httpx.AsyncClient) -> dict | None:
    """Car and Driver road-test rating."""
    try:
        make_slug  = re.sub(r"[^a-z0-9-]", "", make.lower().replace(" ", "-"))
        model_slug = re.sub(r"[^a-z0-9-]", "", model.lower().replace(" ", "-"))
        url = f"https://www.caranddriver.com/{make_slug}/{model_slug}-review"
        r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for ld_tag in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(ld_tag.string or "")
                rat = ld.get("reviewRating") or ld.get("aggregateRating") or {}
                val = rat.get("ratingValue")
                best = rat.get("bestRating", 10)
                if val:
                    return build_source("Car and Driver", "Expert", "&#127941;", "#d0021b",
                                        str(val), str(best))
            except Exception:
                continue
        m = re.search(r'"ratingValue"\s*:\s*"?([\d.]+)"?', r.text)
        if m:
            return build_source("Car and Driver", "Expert", "&#127941;", "#d0021b", m.group(1), "10")
    except Exception:
        pass
    return None


async def score_motortrend(year: str, make: str, model: str, client: httpx.AsyncClient) -> dict | None:
    """MotorTrend review rating."""
    try:
        make_slug  = re.sub(r"[^a-z0-9-]", "", make.lower().replace(" ", "-"))
        model_slug = re.sub(r"[^a-z0-9-]", "", model.lower().replace(" ", "-"))
        url = f"https://www.motortrend.com/cars/{make_slug}/{model_slug}/review/"
        r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
        if r.status_code != 200:
            url = f"https://www.motortrend.com/cars/{make_slug}/{year}/{model_slug}-review/"
            r = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=10)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for ld_tag in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(ld_tag.string or "")
                rat = ld.get("reviewRating") or ld.get("aggregateRating") or {}
                val = rat.get("ratingValue")
                best = rat.get("bestRating", 10)
                if val:
                    return build_source("MotorTrend", "Expert", "&#128664;", "#e8000d",
                                        str(val), str(best))
            except Exception:
                continue
        m = re.search(r'"ratingValue"\s*:\s*"?([\d.]+)"?', r.text)
        if m:
            return build_source("MotorTrend", "Expert", "&#128664;", "#e8000d", m.group(1), "10")
    except Exception:
        pass
    return None


# ─── CANDIDATE SEARCH — CARS ───────────────────────────────────────────────────
_CAR_MAKES = [
    "acura","alfa romeo","aston martin","audi","bentley","bmw","buick","cadillac",
    "chevrolet","chrysler","dodge","ferrari","fiat","ford","genesis","gmc","honda",
    "hyundai","infiniti","jaguar","jeep","kia","lamborghini","land rover","lexus",
    "lincoln","lotus","maserati","mazda","mclaren","mercedes-benz","mini","mitsubishi",
    "nissan","porsche","ram","rolls-royce","subaru","tesla","toyota","volkswagen","volvo",
]

async def candidates_cars(query: str, client: httpx.AsyncClient) -> list:
    """Search NHTSA vehicle database for car candidates."""
    try:
        q = query.lower().strip()
        year_m = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', q)
        year   = year_m.group() if year_m else str(__import__('datetime').datetime.now().year - 1)
        q_no_year = re.sub(r'\b(19|20)\d{2}\b', '', q).strip()

        found_make = next((m for m in _CAR_MAKES if m in q_no_year), None)
        if not found_make:
            # Try partial match on first word
            first_word = q_no_year.split()[0] if q_no_year.split() else ""
            found_make = next((m for m in _CAR_MAKES if m.startswith(first_word) or first_word in m), None)
        if not found_make:
            return []

        model_q = q_no_year.replace(found_make, "").strip()
        r = await client.get(
            f"https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMake/{found_make}",
            params={"format": "json"},
            timeout=8,
        )
        models = r.json().get("Results", [])
        if model_q:
            models = [m for m in models if model_q in m.get("Model_Name", "").lower()]

        results = []
        for m in models[:8]:
            make  = m.get("Make_Name", found_make.title())
            model = m.get("Model_Name", "")
            car_id = f"{year}:{make.lower().replace(' ','-')}:{model.lower().replace(' ','-')}"
            results.append({
                "id":        car_id,
                "category":  "car",
                "title":     f"{year} {make} {model}",
                "year":      year,
                "image_url": None,
                "subtitle":  f"{make} · {year}",
            })
        return results
    except Exception:
        return []


# ─── FULL SCORE — CAR ──────────────────────────────────────────────────────────
async def score_car_by_id(car_id: str, client: httpx.AsyncClient) -> dict | None:
    """Score a car given id = 'year:make:model' (URL-encoded colons as %3A are fine)."""
    try:
        parts = car_id.replace("%3A", ":").split(":", 2)
        if len(parts) < 3:
            return None
        year  = parts[0]
        make  = parts[1].replace("-", " ").title()
        model = parts[2].replace("-", " ").title()

        sources_raw = await asyncio.gather(
            score_nhtsa_safety(year, make, model, client),
            score_edmunds(year, make, model, client),
            score_caranddriver(year, make, model, client),
            score_motortrend(year, make, model, client),
            return_exceptions=True,
        )
        sources = [s for s in sources_raw if s and not isinstance(s, Exception)]

        return {
            "title":     f"{year} {make} {model}",
            "subtitle":  f"{make} · {year}",
            "category":  "car",
            "image_url": None,
            "year":      year,
            "overview":  f"{year} {make} {model} — expert reviews and safety ratings from top automotive sources.",
            "sources":   sources,
        }
    except Exception:
        return None


async def score_rawg_by_id(game_id: str, client: httpx.AsyncClient) -> dict | None:
    if not RAWG_API_KEY:
        return None
    try:
        dr = await client.get(
            f"https://api.rawg.io/api/games/{game_id}",
            params={"key": RAWG_API_KEY},
        )
        detail = dr.json()

        rating        = detail.get("rating", 0)
        ratings_count = detail.get("ratings_count", 0)
        metacritic    = detail.get("metacritic")
        image         = detail.get("background_image")
        genres        = [g["name"] for g in detail.get("genres", [])]
        platforms     = [p["platform"]["name"] for p in detail.get("platforms", [])[:4]]
        description   = re.sub(r"<[^>]+>", "", detail.get("description", ""))[:500]

        sources = []
        if rating > 0:
            sources.append(build_source(
                "RAWG Community", "Users", "&#127918;", "#252525",
                str(round(rating, 1)), "5",
                reviews=f"{ratings_count:,}"
            ))
        if metacritic:
            sources.append(build_source(
                "Metacritic", "Expert", "&#128202;", "#ffcc34",
                str(metacritic), "100"
            ))

        mc_slug = re.sub(r"[^a-z0-9\s-]", "", detail.get("name", "").lower()).strip().replace(" ", "-")
        try:
            mc_url = f"https://www.metacritic.com/game/{mc_slug}/"
            mr = await client.get(mc_url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=8)
            if mr.status_code == 200:
                soup = BeautifulSoup(mr.text, "html.parser")
                ld = soup.find("script", type="application/ld+json")
                if ld:
                    ld_data = json.loads(ld.string)
                    rv = ld_data.get("aggregateRating", {}).get("ratingValue")
                    if rv and not metacritic:
                        sources.append(build_source(
                            "Metacritic", "Expert", "&#128202;", "#ffcc34",
                            str(int(float(rv))), "100"
                        ))
        except Exception:
            pass

        game_name = detail.get("name", "")
        extra_game = await asyncio.gather(
            score_opencritic(game_name, client),
            score_steam(game_id, client),
            score_ign(game_name, "game", client),
            score_gamespot(game_name, client),
            score_polygon(game_name, "game", client),
            return_exceptions=True,
        )
        for s in extra_game:
            if s and not isinstance(s, Exception):
                sources.append(s)

        return {
            "title":     game_name,
            "subtitle":  ", ".join(genres) + (" Â· " + ", ".join(platforms) if platforms else ""),
            "category":  "game",
            "image_url": image,
            "year":      (detail.get("released") or "")[:4],
            "overview":  description,
            "sources":   sources,
        }
    except Exception:
        return None


async def score_tmdb_by_id(item_id: str, media_type: str, client: httpx.AsyncClient) -> dict | None:
    if not TMDB_API_KEY:
        return None
    try:
        base = "https://api.themoviedb.org/3"
        dr   = await client.get(
            f"{base}/{media_type}/{item_id}",
            params={"api_key": TMDB_API_KEY, "language": "en-US"},
        )
        detail = dr.json()

        title      = detail.get("title") or detail.get("name", "")
        year       = (detail.get("release_date") or detail.get("first_air_date", ""))[:4]
        overview   = detail.get("overview", "")
        poster     = (
            f"https://image.tmdb.org/t/p/w500{detail['poster_path']}"
            if detail.get("poster_path") else None
        )
        vote_avg   = detail.get("vote_average", 0)
        vote_count = detail.get("vote_count", 0)
        genres     = [g["name"] for g in detail.get("genres", [])]
        category   = "movie" if media_type == "movie" else "tv"

        sources = []
        if vote_avg > 0:
            sources.append(build_source(
                "TMDB Community", "Users", "&#127916;", "#01b4e4",
                str(round(vote_avg, 1)), "10",
                reviews=f"{vote_count:,}"
            ))

        extra = await asyncio.gather(
            scrape_rt_movie(title, client),
            scrape_metacritic_movie(title, year, client),
            scrape_imdb(title, year, client),
            score_rogerebert(title, year, client),
            score_letterboxd(title, year, client),
            score_ign(title, category, client),
            score_polygon(title, category, client),
            return_exceptions=True,
        )
        for s in extra:
            if s and not isinstance(s, Exception):
                sources.append(s)

        return {
            "title":     title,
            "subtitle":  ", ".join(genres),
            "category":  category,
            "image_url": poster,
            "year":      year,
            "overview":  overview,
            "sources":   sources,
        }
    except Exception:
        return None


async def score_book_by_id(book_id: str, client: httpx.AsyncClient) -> dict | None:
    # book_id is like "works/OL45883W"
    try:
        r = await client.get(f"https://openlibrary.org/{book_id}.json")
        detail = r.json()
        title  = detail.get("title", "")

        # Resolve authors
        authors = []
        for a in detail.get("authors", [])[:2]:
            author_key = a.get("author", {}).get("key") or a.get("key", "")
            if author_key:
                try:
                    ar = await client.get(f"https://openlibrary.org{author_key}.json")
                    name = ar.json().get("name", "")
                    if name:
                        authors.append(name)
                except Exception:
                    pass

        # Ratings
        ratings_r = await client.get(f"https://openlibrary.org/{book_id}/ratings.json")
        ratings   = ratings_r.json()
        avg   = ratings.get("summary", {}).get("average")
        count = ratings.get("summary", {}).get("count", 0)

        sources = []
        if avg:
            sources.append(build_source(
                "Open Library", "Users", "&#128218;", "#2b5797",
                str(round(avg, 1)), "5",
                reviews=f"{count:,}"
            ))

        # Cover image from editions
        image_url = None
        try:
            ed_r = await client.get(
                f"https://openlibrary.org/{book_id}/editions.json",
                params={"limit": 5},
            )
            for ed in ed_r.json().get("entries", []):
                covers = ed.get("covers", [])
                if covers and covers[0] > 0:
                    image_url = f"https://covers.openlibrary.org/b/id/{covers[0]}-L.jpg"
                    break
        except Exception:
            pass

        # Goodreads scrape
        try:
            gr_url = f"https://www.goodreads.com/search?q={title.replace(' ', '+')}"
            gr_r   = await client.get(gr_url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=8)
            if gr_r.status_code == 200:
                soup = BeautifulSoup(gr_r.text, "html.parser")
                rating_el = soup.find("span", class_=re.compile("minirating|average"))
                if rating_el:
                    m = re.search(r"(\d\.\d+)", rating_el.get_text())
                    if m:
                        sources.append(build_source(
                            "Goodreads", "Users", "&#128214;", "#553b08",
                            m.group(1), "5"
                        ))
        except Exception:
            pass

        return {
            "title":     title,
            "subtitle":  ", ".join(authors) if authors else "Unknown Author",
            "category":  "book",
            "image_url": image_url,
            "year":      "",
            "overview":  "",
            "sources":   sources,
        }
    except Exception:
        return None


async def score_music_by_id(mbid: str, client: httpx.AsyncClient) -> dict | None:
    mb_headers = {
        "User-Agent": "Scrawl/1.0 (scrawl.live; contact@scrawl.live)",
        "Accept": "application/json",
    }
    try:
        r = await client.get(
            f"https://musicbrainz.org/ws/2/release/{mbid}",
            params={"fmt": "json", "inc": "artist-credits+labels+recordings"},
            headers=mb_headers,
        )
        detail = r.json()

        title  = detail.get("title", "")
        artist = ""
        credits = detail.get("artist-credit", [])
        if credits:
            artist = credits[0].get("name", "")
        year = (detail.get("date") or "")[:4]

        # Cover art
        image_url = None
        try:
            cover_r = await client.get(
                f"https://coverartarchive.org/release/{mbid}/front",
                follow_redirects=True, timeout=6,
            )
            if cover_r.status_code == 200:
                image_url = str(cover_r.url)
        except Exception:
            pass

        sources = []
        try:
            adm_url = f"https://www.anydecentmusic.com/search/?q={title.replace(' ', '+')}"
            adm_r   = await client.get(adm_url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=8)
            if adm_r.status_code == 200:
                adm_soup = BeautifulSoup(adm_r.text, "html.parser")
                score_el = adm_soup.find("span", class_=re.compile("score|rating"))
                if score_el:
                    score_text = score_el.get_text(strip=True)
                    if re.match(r"^\d+", score_text):
                        sources.append(build_source(
                            "AnyDecentMusic", "Expert", "&#127932;", "#1db954",
                            score_text, "10"
                        ))
        except Exception:
            pass

        extra_music = await asyncio.gather(
            score_pitchfork(title, artist, client),
            score_allmusic(title, artist, client),
            return_exceptions=True,
        )
        for s in extra_music:
            if s and not isinstance(s, Exception):
                sources.append(s)

        return {
            "title":     title,
            "subtitle":  artist,
            "category":  "music",
            "image_url": image_url,
            "year":      year,
            "overview":  f"By {artist}" + (f" Â· {year}" if year else ""),
            "sources":   sources,
        }
    except Exception:
        return None

# âââ ORIGINAL SEARCH HELPERS (name-based, kept for /search endpoint) âââââââââ

async def search_tmdb(query: str, client: httpx.AsyncClient) -> dict | None:
    if not TMDB_API_KEY:
        return None

    base = "https://api.themoviedb.org/3"

    r = await client.get(
        f"{base}/search/multi",
        params={"api_key": TMDB_API_KEY, "query": query, "language": "en-US", "page": 1},
    )
    results = r.json().get("results", [])
    item = next((x for x in results if x.get("media_type") in ("movie", "tv")), None)
    if not item:
        return None

    media_type = item["media_type"]
    item_id    = item["id"]
    return await score_tmdb_by_id(str(item_id), media_type, client)


async def search_rawg(query: str, client: httpx.AsyncClient) -> dict | None:
    if not RAWG_API_KEY:
        return None

    def _norm(s: str) -> list:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        return re.sub(r"[^\w\s]", " ", s.lower()).split()

    r = await client.get(
        "https://api.rawg.io/api/games",
        params={"key": RAWG_API_KEY, "search": query, "page_size": 8},
    )
    results = r.json().get("results", [])
    if not results:
        return None

    q_tokens = set(_norm(query))
    best_idx, best_score = 0, -1.0
    for i, game in enumerate(results):
        t_tokens = set(_norm(game.get("name", "")))
        if not t_tokens:
            continue
        score = len(q_tokens & t_tokens) / max(len(q_tokens), len(t_tokens))
        if score > best_score:
            best_score, best_idx = score, i

    game_id = str(results[best_idx]["id"])
    return await score_rawg_by_id(game_id, client)


async def search_music(query: str, client: httpx.AsyncClient) -> dict | None:
    mb_headers = {
        "User-Agent": "Scrawl/1.0 (scrawl.live; contact@scrawl.live)",
        "Accept": "application/json",
    }

    r = await client.get(
        "https://musicbrainz.org/ws/2/release/",
        params={"query": query, "fmt": "json", "limit": 1},
        headers=mb_headers,
    )
    releases = r.json().get("releases", [])
    if not releases:
        r2 = await client.get(
            "https://musicbrainz.org/ws/2/recording/",
            params={"query": query, "fmt": "json", "limit": 1},
            headers=mb_headers,
        )
        recordings = r2.json().get("recordings", [])
        if not recordings:
            return None
        rec    = recordings[0]
        title  = rec.get("title", "")
        artist = rec.get("artist-credit", [{}])[0].get("name", "") if rec.get("artist-credit") else ""
        year   = (rec.get("first-release-date") or "")[:4]
        mbid   = rec.get("releases", [{}])[0].get("id") if rec.get("releases") else None
    else:
        release = releases[0]
        title   = release.get("title", "")
        artist  = (
            release.get("artist-credit", [{}])[0].get("name", "")
            if release.get("artist-credit") else ""
        )
        year  = (release.get("date") or "")[:4]
        mbid  = release.get("id")

    if mbid:
        result = await score_music_by_id(mbid, client)
        if result:
            return result

    return {
        "title":    title,
        "subtitle": artist,
        "category": "music",
        "image_url": None,
        "year":     year,
        "overview": f"By {artist}" + (f" Â· {year}" if year else ""),
        "sources":  [],
    }


async def search_books(query: str, client: httpx.AsyncClient) -> dict | None:
    r = await client.get(
        "https://openlibrary.org/search.json",
        params={
            "q": query, "limit": 1,
            "fields": "key,title,author_name,first_publish_year,cover_i,"
                      "subject,ratings_average,ratings_count,number_of_pages_median",
        },
    )
    docs = r.json().get("docs", [])
    if not docs:
        return None

    book     = docs[0]
    title    = book.get("title", "")
    authors  = book.get("author_name", [])
    year     = str(book.get("first_publish_year", ""))
    cover_id  = book.get("cover_i")
    image_url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None
    rating    = book.get("ratings_average")
    r_count   = book.get("ratings_count", 0)

    sources = []
    if rating:
        sources.append(build_source(
            "Open Library", "Users", "&#128218;", "#2b5797",
            str(round(rating, 1)), "5",
            reviews=f"{r_count:,}"
        ))

    try:
        gr_url = f"https://www.goodreads.com/search?q={title.replace(' ', '+')}"
        gr_r   = await client.get(gr_url, headers=BROWSER_HEADERS, follow_redirects=True, timeout=8)
        if gr_r.status_code == 200:
            soup = BeautifulSoup(gr_r.text, "html.parser")
            rating_el = soup.find("span", class_=re.compile("minirating|average"))
            if rating_el:
                m = re.search(r"(\d\.\d+)", rating_el.get_text())
                if m:
                    sources.append(build_source(
                        "Goodreads", "Users", "&#128214;", "#553b08",
                        m.group(1), "5"
                    ))
    except Exception:
        pass

    return {
        "title":    title,
        "subtitle": ", ".join(authors[:2]) if authors else "Unknown Author",
        "category": "book",
        "image_url": image_url,
        "year":     year,
        "overview": "",
        "sources":  sources,
    }

# âââ AI OMNISCORE SUMMARY âââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def generate_ai_analysis(
    title: str,
    category: str,
    sources: list,
    overview: str,
    omniscore: int,
) -> dict:
    """Call Claude Haiku to generate OmniScore summary, pros, and cons."""
    if not ANTHROPIC_API_KEY:
        return {
            "summary": "AI SUmmary unavailable. Add your ANTHROPIC_API_KEY to enable this.",
            "pros": [],
            "cons": [],
        }

    import anthropic as _anthropic

    sources_text = "\n".join(
        f"  â¢ {s['name']} ({s['type']}): {s['score']}"
        + (f"/{s['outOf']}" if s.get("outOf") else "")
        + (f" â {s['reviews']} reviews" if s.get("reviews") else "")
        for s in sources
    ) or "  â¢ No third-party scores available yet."

    prompt = f"""You are Scrawl's AI analyst. Scrawl is a review aggregator that gives every product, movie, game, book, and album a single OmniScore out of 100.

Produce a short, punchy analysis for:
  Title:      {title}
  Category:   {category}
  OmniScore:  {omniscore}/100
  Overview:   {overview[:300] if overview else "N/A"}

Scores from across the web:
{sources_text}

Reply ONLY with valid JSON in this exact format â no extra text:
{{
  "summary": "2-3 punchy sentences. Be specific. State what the consensus is and WHY. Mention standout praise or criticism.",
  "pros": ["pro 1 (max 8 words)", "pro 2", "pro 3", "pro 4"],
  "cons": ["con 1 (max 8 words)", "con 2"]
}}"""

    client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=450,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    return {"summary": raw, "pros": [], "cons": []}

# âââ FINALIZE RESULT (shared by /search and /score) ââââââââââââââââââââââââââ

async def finalize(result: dict) -> dict:
    omniscore         = calculate_omniscore(result["sources"])
    result["omniscore"] = omniscore
    result["verdict"]   = verdict_from_score(omniscore)

    ai = await generate_ai_analysis(
        result["title"],
        result["category"],
        result["sources"],
        result.get("overview", ""),
        omniscore,
    )
    result["ai_summary"] = ai.get("summary", "")
    result["pros"]       = ai.get("pros", [])
    result["cons"]       = ai.get("cons", [])
    return result

# âââ ENDPOINTS âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.get("/candidates")
async def get_candidates(
    q:        str = Query(..., min_length=2, description="Search query"),
    category: str = Query("auto", description="Filter: auto | game | movie | tv | music | book | car"),
):
    """
    Fast candidate lookup â returns title, year, poster, category for top matches.
    No scraping, no AI. Used to let the user pick the right item before scoring.
    """
    async with httpx.AsyncClient(timeout=12.0) as client:
        tasks = []
        if category in ("game", "auto"):
            tasks.append(candidates_rawg(q, client))
        if category in ("movie", "tv", "auto"):
            tasks.append(candidates_tmdb(q, client))
        if category in ("book", "auto"):
            tasks.append(candidates_books(q, client))
        if category in ("music", "auto"):
            tasks.append(candidates_music(q, client))
        if category == "car" or (category == "auto" and detect_category(q) == "car"):
            tasks.append(candidates_cars(q, client))

        lists = await asyncio.gather(*tasks, return_exceptions=True)

        all_candidates = []
        for lst in lists:
            if isinstance(lst, list):
                all_candidates.extend(lst)

        # In auto mode: up to 5 per category, 16 total
        if category == "auto":
            seen_cats: dict = {}
            filtered = []
            for c in all_candidates:
                cat = c["category"]
                if seen_cats.get(cat, 0) < 5:
                    filtered.append(c)
                    seen_cats[cat] = seen_cats.get(cat, 0) + 1
            all_candidates = filtered[:16]
        else:
            all_candidates = all_candidates[:10]

    return {"candidates": all_candidates, "query": q, "category": category}


@app.get("/score")
async def score_by_id(
    category: str = Query(..., description="game | movie | tv | music | book"),
    id:       str = Query(..., description="Source-specific item ID"),
):
    """
    Deep score fetch for a specific item by ID.
    Returns OmniScore + per-source scores + AI summary.
    Results cached for CACHE_TTL_HOURS hours.
    """
    cache_key = make_key(f"{category}:{id}")
    cached    = cache_get(cache_key)
    if cached:
        return cached

    async with httpx.AsyncClient(timeout=25.0) as client:
        result = None

        if category == "game":
            result = await score_rawg_by_id(id, client)
        elif category in ("movie", "tv"):
            result = await score_tmdb_by_id(id, category, client)
        elif category == "book":
            result = await score_book_by_id(id, client)
        elif category == "music":
            result = await score_music_by_id(id, client)
        elif category == "car":
            result = await score_car_by_id(id, client)

        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"No score data found for {category} id={id}"
            )

        result = await finalize(result)

    cache_set(cache_key, result)
    return result


@app.get("/search")
async def search(q: str = Query(..., min_length=2, description="Product / movie / game / album to search")):
    """
    Legacy name-based universal search. Auto-detects category.
    Returns OmniScore + per-source scores + AI summary.
    Results cached for CACHE_TTL_HOURS hours.
    """
    key    = make_key(q)
    cached = cache_get(key)
    if cached:
        return cached

    category = detect_category(q)

    async with httpx.AsyncClient(timeout=20.0) as client:
        result = None

        if category == "game":
            result = await search_rawg(q, client)
        elif category == "music":
            result = await search_music(q, client)
        elif category == "book":
            result = await search_books(q, client)
        elif category == "movie":
            result = await search_tmdb(q, client)
        else:
            tasks = await asyncio.gather(
                search_tmdb(q, client),
                search_rawg(q, client),
                search_music(q, client),
                search_books(q, client),
                return_exceptions=True,
            )
            for r in tasks:
                if r and not isinstance(r, Exception):
                    result = r
                    break

        if not result:
            raise HTTPException(status_code=404, detail=f"No results found for '{q}'")

        result = await finalize(result)

    cache_set(key, result)
    return result


@app.get("/health")
async def health():
    return {
        "status":  "ok",
        "service": "Scrawl API",
        "version": "1.3.0",
        "apis": {
            "tmdb":        bool(TMDB_API_KEY),
            "rawg":        bool(RAWG_API_KEY),
            "anthropic":   bool(ANTHROPIC_API_KEY),
            "cloudflare":  bool(CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN),
        },
        "sources": {
            "games":  ["RAWG", "Metacritic", "OpenCritic", "Steam", "IGN"],
            "movies": ["TMDB", "Rotten Tomatoes", "Metacritic", "IMDb", "Roger Ebert", "Letterboxd", "IGN"],
            "music":  ["MusicBrainz", "AnyDecentMusic", "Pitchfork", "AllMusic"],
            "books":  ["Open Library", "Goodreads"],
        },
    }


@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = os.path.join(os.path.dirname(__file__), "frontend", "index.html")
    with open(html_path) as f:
        return f.read()

