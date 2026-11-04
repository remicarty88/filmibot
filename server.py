"""
Веб-обёртка над HdRezkaApi + источник Kinogo.

Запуск:
    python3 server.py
Затем открой http://127.0.0.1:8000/

Источники:
    kinogo  - https://kinogo.sarl  (работает без логина, встроенный плеер)
    hdrezka - зеркало hdrezka (страницы фильмов часто требуют логина)

Переменные окружения:
    KINOGO_ORIGIN    - зеркало Киного (по умолчанию https://kinogo.sarl)
    HDREZKA_ORIGIN   - зеркало hdrezka (по умолчанию https://hdrezka.fi/)
    HDREZKA_USER_ID / HDREZKA_PASSWORD_HASH - cookies hdrezka (если есть аккаунт)
    HDREZKA_PROXY    - свой прокси для страниц сайта; off/none/direct — отключить
    PROXY_GATEWAY    - шлюз прокси-провайдера: prt (по умолчанию) | usa | bra | nzl
    VIDEO_PROXY      - прокси для видеосегментов (по умолчанию = прокси страниц;
                       off/none/direct — пускать видео напрямую)
    PORT             - порт сервера (по умолчанию 8000)
    HOST             - адрес привязки (по умолчанию 0.0.0.0)

Деплой (Railway): команда запуска в Procfile -> gunicorn server:app.
"""
import json
import os
import re
import time
import threading
from urllib.parse import urlparse, urljoin, quote

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, Response, send_file, stream_with_context

from HdRezkaApi import HdRezkaApi
from HdRezkaApi.search import HdRezkaSearch
from HdRezkaApi.types import default_headers, TVSeries

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

KINOGO_ORIGIN = os.environ.get("KINOGO_ORIGIN", "https://kinogo.sarl").rstrip("/")
HDREZKA_ORIGIN = os.environ.get("HDREZKA_ORIGIN", "https://hdrezka.fi/").rstrip("/") + "/"
PROXY_GATEWAYS = {
    "prt": "79.127.168.43",   # в замере оказался самым быстрым
    "usa": "109.204.36.9",
    "bra": "79.127.208.74",
    "nzl": "62.93.164.201",
}
PROXY_LOGIN = "y6Ajblea_1"
PROXY_PASSWORD = "pyR6Nr5Wnm"
PROXY_PORT = "50100"          # 50100 = HTTP(S), 50101 = SOCKS5


def _pick_proxy():
    """Прокси для страниц сайта (на Railway датацентровый IP часто закрыт)."""
    raw = os.environ.get("HDREZKA_PROXY", "").strip()
    if raw.lower() in ("off", "none", "direct"):
        return ""
    if raw:
        return raw
    gw = os.environ.get("PROXY_GATEWAY", "prt").strip().lower()
    host = PROXY_GATEWAYS.get(gw) or PROXY_GATEWAYS["prt"]
    return f"http://{PROXY_LOGIN}:{PROXY_PASSWORD}@{host}:{PROXY_PORT}"


PROXY = _pick_proxy()
# На Railway датацентровый IP не отдаёт видео напрямую, поэтому по умолчанию
# видео тоже идёт через прокси (VIDEO_PROXY=off/direct — пустить напрямую).
_video_raw = os.environ.get("VIDEO_PROXY", "").strip()
if _video_raw.lower() in ("off", "none", "direct"):
    VIDEO_PROXY = ""
elif _video_raw:
    VIDEO_PROXY = _video_raw
else:
    VIDEO_PROXY = PROXY

app = Flask(__name__, static_folder=None)

# ------------------------------------------------------------------ кэш
_cache = {}
_cache_lock = threading.Lock()


def cache_get(key, ttl):
    with _cache_lock:
        item = _cache.get(key)
        if item and time.time() - item[0] < ttl:
            return item[1]
    return None


def cache_set(key, value):
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


# ------------------------------------------------------------------ общее
def make_proxy():
    return {"http": PROXY, "https": PROXY} if PROXY else {}


def make_video_proxy():
    return {"http": VIDEO_PROXY, "https": VIDEO_PROXY} if VIDEO_PROXY else {}


# общий пул keep-alive соединений: убирает TLS-рукопожатие на каждом
# запросе к источнику/прокси (медиа-запросы ускоряются примерно втрое)
_session = None
_session_lock = threading.Lock()


def _get_session():
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=0)
                s.mount("http://", adapter)
                s.mount("https://", adapter)
                _session = s
    return _session


def get_hdrezka_cookies():
    c = {}
    uid = os.environ.get("HDREZKA_USER_ID")
    phash = os.environ.get("HDREZKA_PASSWORD_HASH")
    if uid and phash:
        c.update(HdRezkaApi.make_cookies(uid, phash))
    return c


def detect_source(url):
    host = urlparse(url).netloc.lower()
    return "kinogo" if "kinogo" in host else "hdrezka"


def json_error(message, code=500):
    return jsonify({"ok": False, "error": message}), code


def json_cached(payload, ttl):
    """JSON-ответ с разрешением браузерного кэша (повторное открытие — мгновенно)."""
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "private, max-age=%d" % ttl
    return resp


def http_get(url, timeout=25, video=False):
    r = _get_session().get(url, headers=default_headers, timeout=timeout,
                           proxies=make_video_proxy() if video else make_proxy())
    return r


def abs_url(src):
    if not src:
        return None
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return KINOGO_ORIGIN + src
    return src


# ================================================================== KINOGO
def kinogo_search(query):
    r = _get_session().post(
        KINOGO_ORIGIN + "/",
        data={"do": "search", "subaction": "search", "story": query, "full_search": "1"},
        headers=default_headers, timeout=25, proxies=make_proxy(),
    )
    soup = BeautifulSoup(r.content, "html.parser")
    out = []
    for item in soup.select(".short"):
        a = item.select_one(".short-title")
        if not a:
            continue
        url = a.get("href")
        title = a.get_text(" ", strip=True)
        img = item.select_one(".short-img img") or item.select_one("img")
        image = abs_url(img.get("src")) if img else None
        m = re.search(r"\((\d{4})\)", title)
        out.append({
            "title": title,
            "url": url,
            "image": image,
            "rating": None,
            "year": int(m.group(1)) if m else None,
        })
    return out


def _clean_title(t):
    if not t:
        return None
    t = re.sub(r"\s*смотреть онлайн\s*", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -–—|")


def _brace_block(text, start):
    """Возвращает подстроку от '{' (начиная с start) до парной '}'."""
    i = text.index("{", start)
    depth = 0
    in_str = None
    j = i
    while j < len(text):
        c = text[j]
        if in_str:
            if c == "\\":
                j += 2
                continue
            if c == in_str:
                in_str = None
        elif c in "\"'":
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
        j += 1
    return text[i:]


def _parse_movie_source(html):
    """Фильм: прямые ссылки (hls/dash/mp4) из блока source:{...}."""
    m = re.search(r"source:\s*\{", html)
    if not m:
        return None
    block = _brace_block(html, m.start())

    urls = dict(re.findall(r"\b(dash|dasha|hls|mp4|file|url)\s*:\s*\"([^\"]+)\"", block))
    names = re.search(r'"?names"?\s*:\s*\[([^\]]*)\]', block)
    audio = re.findall(r'"([^"]*)"', names.group(1)) if names else []
    subs = [
        {"url": u, "name": n}
        for u, n in re.findall(r'\{\s*"url"\s*:\s*"([^"]+)"\s*,\s*"name"\s*:\s*"([^"]+)"\s*\}', block)
    ]
    if not any(k in urls for k in ("hls", "mp4", "dash", "dasha", "file", "url")):
        return None
    return {"hls": urls.get("hls"), "mp4": urls.get("mp4") or urls.get("file"),
            "dash": urls.get("dash") or urls.get("dasha"),
            "audio": audio, "subtitles": subs}


def _json_array(text, start):
    """Возвращает подстроку сбалансированного JSON-массива, начиная с '['."""
    depth, in_str, esc = 0, False, False
    i = start
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


def _parse_serial_seasons(html):
    """Сериал: сезоны/эпизоды с прямыми ссылками из массива seasons:[...]."""
    m = re.search(r"\bseasons\s*:\s*\[", html)
    if not m:
        return []
    arr = _json_array(html, html.index("[", m.start()))
    if not arr:
        return []
    try:
        raw = json.loads(arr)
    except Exception:
        return []
    seasons = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        eps = []
        for e in s.get("episodes", []):
            if not isinstance(e, dict) or not (e.get("dash") or e.get("hls")):
                continue
            eps.append({
                "episode": str(e.get("episode")),
                "dash": e.get("dash"),
                "hls": e.get("hls"),
                "mp4": None,
                "audio": [],
                "subtitles": [],
            })
        if eps:
            seasons.append({"season": s.get("season"), "episodes": eps})
    return seasons


def kinogo_film(url):
    r = http_get(url)
    soup = BeautifulSoup(r.content, "html.parser")

    title_el = soup.select_one(".ftitle") or soup.select_one("h1")
    raw_title = title_el.get_text(" ", strip=True) if title_el else None
    name = _clean_title(raw_title)
    if name:
        name = re.sub(r"\s*\((\d{4})\)\s*$", "", name)

    year = None
    if raw_title:
        m = re.search(r"\((\d{4})\)", raw_title)
        if m:
            year = int(m.group(1))

    poster_el = soup.select_one(".fposter img") or soup.select_one(".fposter a img")
    poster = abs_url(poster_el.get("src")) if poster_el else None

    desc = None
    desc_el = soup.select_one(".fdesc")
    if desc_el:
        h = desc_el.find(["h2", "h3"])
        if h:
            h.extract()
        desc = re.sub(r"\s+", " ", desc_el.get_text(" ", strip=True)).strip()

    # категории
    cats = []
    for line in soup.select(".finfo .sd-line"):
        label = line.find("span")
        if label and "Категор" in label.get_text():
            for a in line.find_all("a"):
                t = a.get_text(strip=True)
                if t and t not in cats:
                    cats.append(t)

    # рейтинг: виджет хранит процент (80 -> 8.0)
    rating = None
    bar = soup.select_one(".unit-rating .current-rating")
    if bar:
        m = re.search(r"width:\s*(\d+(?:\.\d+)?)", bar.get("style", ""))
        if m:
            rating = round(float(m.group(1)) / 10, 1)
    if rating is None:
        el = soup.select_one('[itemprop="ratingValue"]')
        if el:
            m = re.search(r"\d+(?:[.,]\d+)?", el.get_text())
            if m:
                rating = float(m.group(0).replace(",", "."))

    # плееры
    players = []
    for i, box in enumerate(soup.select(".fplayer iframe")):
        src = box.get("src")
        if src:
            players.append({"title": f"Плеер {i + 1}", "url": abs_url(src)})
    if not players:
        for i, box in enumerate(soup.find_all("iframe")):
            src = box.get("src")
            if src and ("http" in src):
                players.append({"title": f"Плеер {i + 1}", "url": abs_url(src)})

    # прямые потоки (без рекламного iframe): фильм -> source:{...}, сериал -> seasons:[...]
    sources = []
    seasons = []
    if players:
        from concurrent.futures import ThreadPoolExecutor

        def _fetch_player(url):
            try:
                return http_get(url).text
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=min(8, len(players))) as ex:
            pages = list(ex.map(_fetch_player, [p["url"] for p in players]))

        for p, html in zip(players, pages):
            if not html:
                continue
            src = _parse_movie_source(html)
            if src and (src.get("hls") or src.get("mp4") or src.get("dash")):
                src["title"] = p["title"]
                sources.append(src)
                continue
            if not seasons:
                seasons = _parse_serial_seasons(html)

    if seasons:
        seasons.sort(key=lambda s: s.get("season") or 0)
    is_series = bool(seasons) or "/serial/" in url

    return {
        "source": "kinogo",
        "url": url,
        "origin": KINOGO_ORIGIN,
        "name": name,
        "origName": None,
        "description": desc,
        "releaseYear": year,
        "thumbnail": poster,
        "thumbnailHQ": poster,
        "rating": {"value": rating, "votes": None},
        "type": "video.tv_series" if is_series else "video.movie",
        "category": "category." + (urlparse(url).path.strip("/").split("/")[0] or ""),
        "categories": cats,
        "isSeries": is_series,
        "sources": sources,
        "players": players,
        "translators": {},
        "seasons": seasons,
    }


# ================================================================== HDREZKA
def hdrezka_search(query, find_all=False):
    search = HdRezkaSearch(HDREZKA_ORIGIN, proxy=make_proxy(), cookies=get_hdrezka_cookies())
    results = search(query, find_all=find_all)
    items = results.all if find_all else results
    out = []
    for it in items:
        out.append({
            "title": it.get("title"),
            "url": it.get("url"),
            "image": it.get("image"),
            "rating": it.get("rating"),
            "year": None,
        })
    return out


def hdrezka_film(url):
    rezka = HdRezkaApi(url, proxy=make_proxy(), cookies=get_hdrezka_cookies())
    if not rezka.ok:
        raise rezka.exception

    def safe(fn, default=None):
        try:
            return fn()
        except Exception:
            return default

    is_series = safe(lambda: rezka.type == TVSeries, False)
    return {
        "source": "hdrezka",
        "url": rezka.url,
        "origin": rezka.origin,
        "id": safe(lambda: rezka.id),
        "name": safe(lambda: rezka.name),
        "origName": safe(lambda: rezka.origName),
        "description": safe(lambda: rezka.description),
        "releaseYear": safe(lambda: rezka.releaseYear),
        "thumbnail": safe(lambda: rezka.thumbnail),
        "thumbnailHQ": safe(lambda: rezka.thumbnailHQ),
        "rating": safe(lambda: {"value": rezka.rating.value, "votes": rezka.rating.votes}),
        "type": safe(lambda: str(rezka.type)),
        "category": safe(lambda: str(rezka.category)),
        "categories": [],
        "isSeries": is_series,
        "players": [],
        "translators": safe(lambda: rezka.translators, {}),
        "otherParts": safe(lambda: rezka.otherParts, []),
        "seasons": safe(lambda: rezka.episodesInfo, []) if is_series else [],
    }


# ================================================================== HTTP API
@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "index.html"))


# ---------------------------------------------------------------- PWA (манифест, иконки, SW)
@app.route("/manifest.json")
def manifest():
    return send_file(
        os.path.join(BASE_DIR, "manifest.json"),
        mimetype="application/manifest+json",
    )


@app.route("/sw.js")
def service_worker():
    r = send_file(os.path.join(BASE_DIR, "sw.js"), mimetype="application/javascript")
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    r.headers["Service-Worker-Allowed"] = "/"
    return r


@app.route("/icon-<string:name>.png")
def icon(name):
    path = os.path.join(BASE_DIR, "icon-%s.png" % name)
    if not os.path.exists(path):
        return ("", 404)
    return send_file(path, mimetype="image/png")


@app.route("/apple-touch-icon.png")
def apple_icon():
    return send_file(os.path.join(BASE_DIR, "apple-touch-icon.png"), mimetype="image/png")



# ================================================================== КАТАЛОГ ГЛАВНОЙ
HOME_ROWS = [
    ("Боевики", "боевик"),
    ("Комедии", "комедия"),
    ("Фантастика", "фантастика"),
    ("Ужасы", "ужасы"),
    ("Драмы", "драма"),
    ("Триллеры", "триллер"),
    ("Мультфильмы", "мультфильм"),
    ("Аниме", "аниме"),
    ("Сериалы", "сериал"),
]


def kinogo_list(url):
    """Парсит список карточек (.short) с любой страницы каталога."""
    r = http_get(url)
    soup = BeautifulSoup(r.content, "html.parser")
    out = []
    for item in soup.select(".short"):
        a = item.select_one(".short-title")
        if not a or not a.get("href"):
            continue
        img = item.select_one(".short-img img") or item.select_one("img")
        image = abs_url(img.get("src")) if img else None
        if not image:
            continue
        title = a.get_text(" ", strip=True)
        m = re.search(r"\((\d{4})\)", title)
        out.append({
            "title": re.sub(r"\s*\(\d{4}\)\s*$", "", _clean_title(title) or title),
            "url": a.get("href"),
            "image": image,
            "year": int(m.group(1)) if m else None,
        })
    return out


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


@app.route("/api/home")
def api_home():
    cached = cache_get("home", 1800)
    if cached is not None:
        return jsonify({"ok": True, "rows": cached})

    from concurrent.futures import ThreadPoolExecutor
    jobs = [("Новинки", lambda: kinogo_list(KINOGO_ORIGIN + "/") + _safe(lambda: kinogo_list(KINOGO_ORIGIN + "/page/2/"), []))]
    for title, q in HOME_ROWS:
        jobs.append((title, (lambda q=q: kinogo_search(q))))

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [(t, ex.submit(_safe, fn, [])) for t, fn in jobs]
        rows = []
        seen = set()
        for title, fut in futures:
            items = []
            for it in fut.result():
                if it.get("url") in seen or not it.get("image"):
                    continue
                seen.add(it["url"])
                items.append(it)
            if items:
                rows.append({"title": title, "items": items[:24]})
    if rows:
        cache_set("home", rows)
    return jsonify({"ok": True, "rows": rows})


@app.route("/api/qualities")
def api_qualities():
    """Диагностика: какие разрешения реально отдаёт источник (есть ли 1080p)."""
    url = (request.args.get("url") or "").strip()
    if not url:
        return json_error("Не передан url", 400)
    try:
        film = cache_get(f"film:{url}", 600) or kinogo_film(url)
        report = []
        for src in film.get("sources", []):
            entry = {"player": src.get("title"), "hls": src.get("hls"), "mp4": src.get("mp4"), "levels": []}
            if src.get("hls"):
                txt = http_get(src["hls"]).text
                for m in re.finditer(r"#EXT-X-STREAM-INF:([^\n]+)", txt):
                    attrs = m.group(1)
                    res = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
                    bw = re.search(r"BANDWIDTH=(\d+)", attrs)
                    entry["levels"].append({
                        "resolution": f"{res.group(1)}x{res.group(2)}" if res else None,
                        "height": int(res.group(2)) if res else None,
                        "bandwidth": int(bw.group(1)) if bw else None,
                    })
                heights = [l["height"] for l in entry["levels"] if l["height"]]
                entry["max_height"] = max(heights) if heights else None
                entry["has_1080"] = bool(heights and max(heights) >= 1000)
            report.append(entry)
        return jsonify({"ok": True, "name": film.get("name"), "sources": report})
    except Exception as e:
        return json_error(f"{type(e).__name__}: {e}")


@app.route("/api/config")
def api_config():
    return jsonify({
        "ok": True,
        "sources": {"kinogo": KINOGO_ORIGIN, "hdrezka": HDREZKA_ORIGIN},
        "defaultSource": "kinogo",
        "proxy": bool(PROXY),
        "hdrezkaAuthorized": bool(get_hdrezka_cookies()),
    })


@app.route("/api/search")
def api_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return json_error("Пустой запрос", 400)
    source = request.args.get("source") or "kinogo"
    find_all = request.args.get("all") in ("1", "true", "yes")

    key = f"search:{source}:{query}:{find_all}"
    cached = cache_get(key, 300)
    if cached is not None:
        return jsonify({"ok": True, "results": cached, "source": source})

    try:
        if source == "kinogo":
            results = kinogo_search(query)
        else:
            results = hdrezka_search(query, find_all)
        cache_set(key, results)
        return jsonify({"ok": True, "results": results, "source": source})
    except Exception as e:
        return json_error(f"{type(e).__name__}: {e}")


@app.route("/api/film")
def api_film():
    url = (request.args.get("url") or "").strip()
    if not url:
        return json_error("Не передан url", 400)

    key = f"film:{url}"
    cached = cache_get(key, 600)
    if cached is not None:
        return json_cached({"ok": True, "film": cached}, 600)

    try:
        if detect_source(url) == "kinogo":
            film = kinogo_film(url)
        else:
            film = hdrezka_film(url)
        cache_set(key, film)
        return json_cached({"ok": True, "film": film}, 600)
    except Exception as e:
        return json_error(f"{type(e).__name__}: {e}")


@app.route("/api/stream")
def api_stream():
    url = (request.args.get("url") or "").strip()
    if not url:
        return json_error("Не передан url", 400)

    key = f"stream:{url}:{request.args.get('season')}:{request.args.get('episode')}:{request.args.get('translation')}"
    cached = cache_get(key, 300)
    if cached is not None:
        return json_cached({"ok": True, "stream": cached}, 300)

    try:
        if detect_source(url) == "kinogo":
            film = cache_get(f"film:{url}", 600) or kinogo_film(url)
            if not film.get("sources"):
                return json_error("Прямой поток не найден")
            data = {"kind": "hls", "origin": KINOGO_ORIGIN, "sources": film["sources"]}
        else:
            rezka = HdRezkaApi(url, proxy=make_proxy(), cookies=get_hdrezka_cookies())
            if not rezka.ok:
                raise rezka.exception
            season = request.args.get("season")
            episode = request.args.get("episode")
            translation = request.args.get("translation")
            if rezka.type == TVSeries:
                if not season or not episode:
                    return json_error("Для сериала нужны season и episode", 400)
                stream = rezka.getStream(int(season), int(episode), translation=translation)
            else:
                stream = rezka.getStream(translation=translation)
            subtitles = {}
            try:
                subtitles = dict(stream.subtitles.subtitles)
            except Exception:
                pass
            data = {
                "kind": "video",
                "origin": rezka.origin,
                "translator_id": stream.translator_id,
                "videos": stream.videos,
                "subtitles": subtitles,
            }
        cache_set(key, data)
        return json_cached({"ok": True, "stream": data}, 300)
    except Exception as e:
        return json_error(f"{type(e).__name__}: {e}")


def _proxy_url(u):
    return "/api/proxy?url=" + quote(u, safe="")


def _rewrite_m3u8(text, base):
    """Абсолютизирует и заворачивает в прокси все URI внутри плейлиста."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
        elif s.startswith("#"):
            out.append(re.sub(r'URI="([^"]+)"',
                              lambda m: 'URI="%s"' % _proxy_url(urljoin(base, m.group(1))), line))
        else:
            out.append(_proxy_url(urljoin(base, s)))
    return "\n".join(out)


def _rewrite_mpd(text, mpd_url):
    """Абсолютизирует и заворачивает в прокси все URL внутри DASH-манифеста."""
    import html as _html

    bases = [_html.unescape(b).strip() for b in re.findall(r"<BaseURL>\s*([^<]+?)\s*</BaseURL>", text)]
    base = bases[0] if bases else mpd_url.rsplit("/", 1)[0] + "/"
    text = re.sub(r"<BaseURL>\s*[^<]+?\s*</BaseURL>\s*", "", text)

    def wrap(u):
        u = _html.unescape(u)
        if u.startswith("data:"):
            return u
        # $Number$/$Time$/$Bandwidth$ должны остаться как есть — dash.js подставит их сам
        return _proxy_url(urljoin(base, u)).replace("%24", "$")

    for attr in ("initialization", "media", "sourceURL"):
        text = re.sub(
            r'(\b%s=")([^"]+)(")' % attr,
            lambda m: m.group(1) + wrap(m.group(2)) + m.group(3),
            text,
        )
    return text


@app.route("/api/dash")
def api_dash():
    target = request.args.get("url")
    if not target:
        return json_error("Не передан url", 400)
    try:
        r = http_get(target, video=True)
        if r.status_code != 200:
            return json_error(f"Источник вернул {r.status_code}", r.status_code)
        body = _rewrite_mpd(r.text, target)
        return Response(body, headers={"Content-Type": "application/dash+xml",
                                       "Cache-Control": "no-store"})
    except Exception as e:
        return json_error(f"{type(e).__name__}: {e}")


@app.route("/api/proxy")
def api_proxy():
    target = request.args.get("url")

    headers = dict(default_headers)
    referer = request.args.get("ref")
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = referer.rstrip("/")
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    try:
        upstream = _get_session().get(target, headers=headers, stream=True, timeout=30,
                                      proxies=make_video_proxy())
    except Exception as e:
        return json_error(f"{type(e).__name__}: {e}")

    ctype = upstream.headers.get("Content-Type", "").lower()
    it = upstream.iter_content(chunk_size=64 * 1024)
    try:
        first = next(it)
    except StopIteration:
        first = b""

    # плейлист HLS -> переписываем все ссылки на прокси
    if "mpegurl" in ctype or first[:7] == b"#EXTM3U":
        body = _rewrite_m3u8((first + b"".join(it)).decode("utf-8", "ignore"), target)
        return Response(body, status=upstream.status_code,
                        headers={"Content-Type": "application/vnd.apple.mpegurl",
                                 "Cache-Control": "no-store"})

    resp_headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for h in ("Content-Type", "Content-Length", "Content-Range"):
        if h in upstream.headers:
            resp_headers[h] = upstream.headers[h]

    def gen():
        try:
            yield first
            for chunk in it:
                yield chunk
        finally:
            upstream.close()  # вернуть соединение в пул (или закрыть при обрыве)

    return Response(stream_with_context(gen()), status=upstream.status_code, headers=resp_headers)


@app.route("/api/subtitle")
def api_subtitle():
    target = request.args.get("url")
    if not target:
        return json_error("Не передан url", 400)
    try:
        r = _get_session().get(target, headers=default_headers, timeout=30, proxies=make_proxy())
        return Response(r.content, status=r.status_code,
                        headers={"Content-Type": "text/vtt; charset=utf-8", "Cache-Control": "no-store"})
    except Exception as e:
        return json_error(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"Сервер: http://{host}:{port}/")
    print(f"Киного: {KINOGO_ORIGIN}   HDRezka: {HDREZKA_ORIGIN}")
    app.run(host=host, port=port, threaded=True)
