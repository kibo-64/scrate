"""
Scrate API â Backend
Universal review aggregator: movies, TV, games, music, books, products
OmniScore = weighted AI-calculated master score across all sources
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import json
import os
import re
import asyncio
import hashlib
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# âââ CONFIG ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

TMDB_API_KEY      = os.getenv("TMDB_API_KEY", "")
RAWG_API_KEY      = os.getenv("RAWG_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# How long to keep cached results before refreshing (hours)
CACHE_TTL_HOURS = 72

# In-memory cache: {hash: {data, expires_at}}
_cache: dict = {}

# âââ APP âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

app = FastAPI(title="Scrate API", version="1.1.0")

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
                        "Metacritic", "Expert", "ð", "#ffcc34",
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
                        "Rotten Tomatoes", "Expert", "ð", "#fa320a",
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
                    "IMDb", "Users", "â­", "#f5c518",
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
            params={"key": RAWG_API_KEY, "search": query, "page_size": 8},
        )
        results = []
        for g in r.json().get("results", [])[:8]:
            genres = [gen["name"] for gen in g.get("genres", [])[:2]]
            results.append({
                "id":        str(g["id"]),
                "category":  "game",
                "title":     g.get("name", ""),
                "year":      (g.get("released") or "")[:4],
                "image_url": g.get("background_image"),
                "subtitle":  ", ".join(genres),
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
            results.append({
                "id":        str(item["id"]),
                "category":  item["media_type"],
                "title":     title,
                "year":      year,
                "image_url": poster,
                "subtitle":  (item.get("overview") or "")[:120],
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
        "User-Agent": "Scrate/1.0 (scrate.io; contact@scrate.io)",
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

# âââ SCORE BY ID HELPERS (deep fetch) ââââââââââââââââââââââââââââââââââââââââ

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
                "RAWG Community", "Users", "ð®", "#252525",
                str(round(rating, 1)), "5",
                reviews=f"{ratings_count:,}"
            ))
        if metacritic:
            sources.append(build_source(
                "Metacritic", "Expert", "ð", "#ffcc34",
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
                            "Metacritic", "Expert", "ð", "#ffcc34",
                            str(int(float(rv))), "100"
                        ))
        except Exception:
            pass

        return {
            "title":     detail.get("name", ""),
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
                "TMDB Community", "Users", "ð¬", "#01b4e4",
                str(round(vote_avg, 1)), "10",
                reviews=f"{vote_count:,}"
            ))

        extra = await asyncio.gather(
            scrape_rt_movie(title, client),
            scrape_metacritic_movie(title, year, client),
            scrape_imdb(title, year, client),
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
                "Open Library", "Users", "ð", "#2b5797",
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
                            "Goodreads", "Users", "ð", "#553b08",
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
        "User-Agent": "Scrate/1.0 (scrate.io; contact@scrate.io)",
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
                            "AnyDecentMusic", "Expert", "ð¼", "#1db954",
                            score_text, "10"
                        ))
        except Exception:
            pass

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

    r = await client.get(
        "https://api.rawg.io/api/games",
        params={"key": RAWG_API_KEY, "search": query, "page_size": 1},
    )
    results = r.json().get("results", [])
    if not results:
        return None

    game_id = str(results[0]["id"])
    return await score_rawg_by_id(game_id, client)


async def search_music(query: str, client: httpx.AsyncClient) -> dict | None:
    mb_headers = {
        "User-Agent": "Scrate/1.0 (scrate.io; contact@scrate.io)",
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
            "Open Library", "Users", "ð", "#2b5797",
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
                        "Goodreads", "Users", "ð", "#553b08",
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
            "summary": "AI summary unavailable. Add your ANTHROPIC_API_KEY to enable this.",
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

    prompt = f"""You are Scrate's AI analyst. Scrate is a review aggregator that gives every product, movie, game, book, and album a single OmniScore out of 100.

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
    category: str = Query("auto", description="Filter: auto | game | movie | tv | music | book"),
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

        lists = await asyncio.gather(*tasks, return_exceptions=True)

        all_candidates = []
        for lst in lists:
            if isinstance(lst, list):
                all_candidates.extend(lst)

        # In auto mode: up to 4 per category, 12 total
        if category == "auto":
            seen_cats: dict = {}
            filtered = []
            for c in all_candidates:
                cat = c["category"]
                if seen_cats.get(cat, 0) < 4:
                    filtered.append(c)
                    seen_cats[cat] = seen_cats.get(cat, 0) + 1
            all_candidates = filtered[:12]
        else:
            all_candidates = all_candidates[:8]

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
        "service": "Scrate API",
        "version": "1.1.0",
        "apis": {
            "tmdb":      bool(TMDB_API_KEY),
            "rawg":      bool(RAWG_API_KEY),
            "anthropic": bool(ANTHROPIC_API_KEY),
        },
    }


@app.get("/")
async def root():
    return {"message": "Scrate API is running. Use GET /candidates?q=zelda or GET /score?category=game&id=123"}
