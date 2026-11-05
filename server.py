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
    HDREZKA_ORIGIN   - зеркало hdrezka (по умолчанию https://hdrezka.my/)
    HDREZKA_USER_ID / HDREZKA_PASSWORD_HASH - cookies hdrezka (если есть аккаунт)
    HDREZKA_PROXY    - свой прокси для страниц сайта; off/none/direct — отключить
    PROXY_GATEWAY    - шлюз прокси-провайдера: prt (по умолчанию) | usa | bra | nzl
    VIDEO_PROXY      - прокси для видеосегментов (по умолчанию = прокси страниц;
                       off/none/direct — пускать видео напрямую)
    PROXY_CONN_LIMIT - сколько запросов к прокси держать одновременно (по умолчанию 4;
                       0 — без ограничения). Провайдер отвечает 429 при перегрузе.
    PORT             - порт сервера (по умолчанию 8000)
    HOST             - адрес привязки (по умолчанию 0.0.0.0)

Деплой (Railway): команда запуска в Procfile -> gunicorn server:app.
"""
import io
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

try:  # gzip/brotli-сжатие ответов (HTML, JSON) — быстрее загрузка на мобильных
    from flask_compress import Compress
except Exception:
    Compress = None

try:  # ресайз постеров на сервере: карточки получают резкие WebP вместо «мыла»
    from PIL import Image
except Exception:
    Image = None

from HdRezkaApi import HdRezkaApi
from HdRezkaApi.types import default_headers

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

KINOGO_ORIGIN = os.environ.get("KINOGO_ORIGIN", "https://kinogo.sarl").rstrip("/")
HDREZKA_ORIGIN = os.environ.get("HDREZKA_ORIGIN", "https://hdrezka.my/").rstrip("/") + "/"
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
# На Railway датацентровый IP часто не отдаёт видео напрямую, поэтому прокси
# можно задать явно (VIDEO_PROXY=off/direct — пустить напрямую).
_video_raw = os.environ.get("VIDEO_PROXY", "").strip()
if _video_raw.lower() in ("off", "none", "direct"):
    VIDEO_PROXY = ""
elif _video_raw:
    VIDEO_PROXY = _video_raw
else:
    VIDEO_PROXY = PROXY

# Транспорт видео:
#   auto (по умолчанию) — сначала пробуем напрямую (вне датацентра это в разы
#     быстрее), при блокировке CDN автоматически переходим на прокси;
#   on  — всегда напрямую;  off — всегда через прокси.
VIDEO_DIRECT = (os.environ.get("VIDEO_DIRECT", "auto").strip().lower() or "auto")

app = Flask(__name__, static_folder=None)

if Compress is not None:
    # сжимаем только текст/JSON — видео-сегменты и плейлисты идут как есть
    app.config["COMPRESS_MIMETYPES"] = [
        "text/html", "text/css", "application/json",
        "application/javascript", "image/svg+xml",
    ]
    app.config["COMPRESS_MIN_SIZE"] = 1024
    Compress(app)

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


# CDN видео (в т.ч. hdrezka) фильтрует по TLS-отпечатку: обычный requests
# получает 410 Gone, а curl_cffi с имперсонацией — 200. Сессия на поток:
# keep-alive для сегментов + потокобезопасность.
_video_local = threading.local()

# Какой транспорт работает для конкретного потока: hdrezka-сегменты требуют
# TLS-имперсонацию curl_cffi, а kinogo — обычный requests (оба на одном CDN).
# Запоминаем результат по «корню» потока (папка .../*.mp4/ или каталог mpd),
# чтобы не делать холостой запрос на каждый сегмент.
_transport_memo = {}
_transport_lock = threading.Lock()

# Нужен ли прокси для конкретного потока: None — неизвестно, True/False — установлено.
# В режиме auto прямой доступ пробуем первым, а при блокировке CDN запоминаем это,
# чтобы не делать холостую попытку на каждый следующий сегмент.
_video_proxy_memo = {}
_video_proxy_lock = threading.Lock()
_direct_state = {"strikes": 0, "off_until": 0.0}

# Прокси-провайдер режет по лимиту одновременных соединений: при пачке
# параллельных запросов он отвечает HTTP 429 на CONNECT, и видео перестаёт
# грузиться/перематываться. Телефон (iPhone, нативный HLS) открывает сразу
# несколько соединений, поэтому упираемся в лимит. Пропускаем через прокси не
# больше PROXY_CONN_LIMIT одновременных запросов (0 — без ограничения).
try:
    PROXY_CONN_LIMIT = int(os.environ.get("PROXY_CONN_LIMIT", "4") or 4)
except ValueError:
    PROXY_CONN_LIMIT = 4
_proxy_slots = (threading.BoundedSemaphore(PROXY_CONN_LIMIT)
                if PROXY_CONN_LIMIT > 0 and VIDEO_PROXY else None)


def _stream_root(url):
    m = re.match(r"^(.*?\.mp4/)", url)
    if m:
        return m.group(1)
    p = urlparse(url)
    return p.scheme + "://" + p.netloc + p.path.rsplit("/", 1)[0] + "/"


def _memo_set(root, value):
    with _transport_lock:
        if len(_transport_memo) > 4096:  # не даём словарю расти бесконечно
            _transport_memo.clear()
        _transport_memo[root] = value


def hint_transport(url, source):
    """Подсказка от плеера (src=kinogo|hdrezka): выбирает транспорт сразу,
    без холостого запроса на первый сегмент."""
    if _cffi is None or not url or source not in ("kinogo", "hdrezka"):
        return
    with _transport_lock:
        if len(_transport_memo) > 4096:
            _transport_memo.clear()
        _transport_memo.setdefault(_stream_root(url), source == "hdrezka")


def _direct_allowed():
    if VIDEO_DIRECT == "off":
        return False
    if VIDEO_DIRECT == "on":
        return True
    with _video_proxy_lock:
        return time.time() >= _direct_state["off_until"]


def _proxy_memo_set(root, value):
    with _video_proxy_lock:
        if len(_video_proxy_memo) > 4096:
            _video_proxy_memo.clear()
        _video_proxy_memo[root] = value


def _note_direct(ok):
    with _video_proxy_lock:
        if ok:
            _direct_state["strikes"] = 0
        else:
            _direct_state["strikes"] += 1
            if _direct_state["strikes"] >= 3:  # CDN стабильно режет — 15 мин не пробуем
                _direct_state["off_until"] = time.time() + 900


def _note_proxy_limited():
    """Прокси ответил 429/5xx — снимаем паузу с прямого доступа: не бьём дальше
    по уже перегруженному прокси, ещё раз пробуем напрямую."""
    with _video_proxy_lock:
        _direct_state["off_until"] = 0.0
        _direct_state["strikes"] = 0


# 429 — прокси-провайдер режет по лимиту; 407/408/5xx — временный сбой на пути.
_RETRY_CODES = {407, 408, 425, 429, 500, 502, 503, 504}


def _video_ok(r):
    """Годен ли ответ как видеопоток (а не страница-заглушка CDN или ошибка прокси)."""
    if r is None or r.status_code in (403, 404, 410, 451) or r.status_code in _RETRY_CODES:
        return False
    return "text/html" not in (r.headers.get("Content-Type") or "").lower()


def _retry_get(fn, attempts=3, delay=0.5):
    """GET с повтором при временных ошибках (лимит прокси, 5xx).

    Между попытками пауза с ростом — короткие всплески 429 так проходят.
    Возвращает последний ответ; если все попытки упали с исключением — бросает его.
    """
    last, last_exc = None, None
    for i in range(attempts):
        last_exc = None
        try:
            last = fn()
        except Exception as e:
            last, last_exc = None, e
        if last is not None and last.status_code not in _RETRY_CODES:
            return last
        if i + 1 < attempts:
            if last is not None:      # промежуточный «плохой» ответ не держим
                last.close()
                last = None
            time.sleep(delay * (i + 1))
    if last is not None:
        return last
    if last_exc is not None:
        raise last_exc
    return None


def _cffi_session(proxied):
    attr = "proxy_session" if proxied else "direct_session"
    s = getattr(_video_local, attr, None)
    if s is None:
        s = _cffi.Session(impersonate=HDREZKA_IMPERSONATE,
                          proxies=(make_video_proxy() or None) if proxied else None)
        setattr(_video_local, attr, s)
    return s


def _cffi_try(headers, url, stream, timeout, proxied):
    try:
        return _cffi_session(proxied).get(url, headers=headers, stream=stream, timeout=timeout)
    except Exception:
        return None


def video_get(url, headers=None, stream=False, timeout=30):
    headers = dict(headers or {})
    if _cffi is not None:
        root = _stream_root(url)
        with _transport_lock:
            prefer = _transport_memo.get(root)
        with _video_proxy_lock:
            need_proxy = _video_proxy_memo.get(root)
        if prefer is not False:  # неизвестно или известно, что нужен curl_cffi
            # User-Agent задаёт имперсонация: свой UA конфликтует с TLS-отпечатком
            # (CDN отвечает 410), поэтому его выкидываем.
            cffi_headers = dict(headers)
            cffi_headers.pop("User-Agent", None)

            # 1) прямое подключение — обычно в разы быстрее прокси
            if _direct_allowed() and need_proxy is not True:
                r = _cffi_try(cffi_headers, url, stream, min(timeout, 6), proxied=False)
                if _video_ok(r):
                    _note_direct(True)
                    _proxy_memo_set(root, False)
                    if prefer is None:
                        _memo_set(root, True)
                    return r
                if r is not None:
                    r.close()
                if VIDEO_PROXY:
                    _note_direct(False)
                    if VIDEO_DIRECT != "on":
                        _proxy_memo_set(root, True)

            # 2) через прокси (прямой доступ закрыт или выключен).
            # Повторяем с паузой: провайдер отдаёт 429 при всплеске запросов.
            if VIDEO_PROXY and VIDEO_DIRECT != "on":
                r = _retry_get(lambda: _cffi_try(cffi_headers, url, stream, timeout,
                                                 proxied=True), attempts=3, delay=0.6)
                if _video_ok(r):
                    _proxy_memo_set(root, True)
                    if prefer is None:
                        _memo_set(root, True)
                    return r
                if r is not None:
                    if r.status_code in _RETRY_CODES:
                        _note_proxy_limited()
                    r.close()
            _memo_set(root, False)
    return _retry_get(
        lambda: _get_session().get(url, headers=headers or None, stream=stream,
                                   timeout=timeout, proxies=make_video_proxy()),
        attempts=3, delay=0.6)


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


def http_get(url, timeout=25, video=False, use_proxy=True):
    # use_proxy=False — идём напрямую: страницы плееров-эмбедов (nextembed,
    # ortified и пр.) блокируют IP прокси и отдают 422 с пустым телом.
    proxies = None
    if use_proxy:
        proxies = make_video_proxy() if video else make_proxy()
    return _retry_get(
        lambda: _get_session().get(url, headers=default_headers, timeout=timeout,
                                   proxies=proxies),
        attempts=3 if use_proxy else 1, delay=0.6)


# Страницы плееров-эмбедов (nextembed, ortified, stravers …) блокируют IP прокси
# (HTTP 422 с пустым телом), а сам сайт источника закрыт Cloudflare и с
# датацентрового IP (Railway) без прокси не открывается. Поэтому для таких
# страниц пробуем оба транспорта и запоминаем рабочий отдельно по каждому хосту.
# EMBED_TRANSPORT=auto|direct|proxy — принудительный режим, если нужно.
EMBED_TRANSPORT = os.environ.get("EMBED_TRANSPORT", "auto").strip().lower()
_embed_mode = {}
_embed_lock = threading.Lock()


def fetch_embed(url, getter, timeout=30):
    """HTML страницы плеера. getter(url, use_proxy=<bool>, timeout=<int>) -> response."""
    if EMBED_TRANSPORT == "direct":
        order = [False]
    elif EMBED_TRANSPORT == "proxy":
        order = [True]
    else:
        host = urlparse(url).netloc
        with _embed_lock:
            pref = _embed_mode.get(host)
        order = [True, False] if pref == "proxy" else [False, True]

    for use_proxy in order:
        try:
            r = getter(url, use_proxy=use_proxy, timeout=timeout)
        except Exception as e:
            app.logger.info("embed %s via %s: %s", url, "proxy" if use_proxy else "direct", e)
            continue
        if r.status_code == 200 and r.text and len(r.text) > 200:
            if EMBED_TRANSPORT == "auto":
                with _embed_lock:
                    if len(_embed_mode) > 512:
                        _embed_mode.clear()
                    _embed_mode[host] = "proxy" if use_proxy else "direct"
            return r.text
        app.logger.info("embed %s via %s -> %s (%d байт)", url,
                        "proxy" if use_proxy else "direct", r.status_code, len(r.content))
    return None


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


def _perep_playlist(filepath, referer, headers):
    """perepolokha: variant.filepath — это JSON с готовым HLS-плейлистом."""
    if not filepath:
        return None
    try:
        data = _hd_get(filepath, referer=referer, headers=headers, timeout=30).json()
    except Exception:
        return None
    srcs = data.get("sources") or []
    link = next((s.get("link") for s in srcs if isinstance(s, dict) and s.get("link")), None)
    if not link:
        return None
    subs = [{"url": s.get("url"), "name": s.get("name") or s.get("label") or "Субтитры"}
            for s in (data.get("subtitles") or []) if isinstance(s, dict) and s.get("url")]
    return link, subs


def _parse_perepolokha(iframe_url, html):
    """Плеер perepolokha.link: прямые HLS отдаёт его balancer-api (catalog-api).

    Страница игрока — SPA без ссылок в HTML, поэтому берём потоки через API:
    episodes?content-id=<id> -> episodeVariants[].filepath -> JSON с sources[].link.
    Возвращает (sources, seasons).
    """
    m = re.search(r"window\.ENV_BASE_URL\s*=\s*'([^']+)'", html)
    mid = re.search(r"[?&]movie_id=(\d+)", iframe_url)
    tok = re.search(r"DLE-API-TOKEN'\s*:\s*'([^']+)'", html)
    if not (m and mid and tok):
        return [], []
    base = m.group(1).rstrip("/")            # .../proxy/playlists
    hdr = {"DLE-API-TOKEN": tok.group(1), "Origin": "https://perepolokha.link"}
    rid = re.search(r"Iframe-Request-Id'\s*:\s*'([^']+)'", html)
    if rid:
        hdr["Iframe-Request-Id"] = rid.group(1)

    try:
        eps = _hd_get(f"{base}/catalog-api/episodes?content-id={mid.group(1)}",
                      referer=iframe_url, headers=hdr, timeout=30).json()
    except Exception:
        return [], []
    if not isinstance(eps, list):
        return [], []
    eps = [e for e in eps if isinstance(e, dict)]
    from concurrent.futures import ThreadPoolExecutor

    # фильм: одна «серия», каждая озвучка (variant) -> отдельный источник
    if len(eps) == 1:
        variants = [v for v in (eps[0].get("episodeVariants") or []) if isinstance(v, dict)]

        def job(v):
            got = _perep_playlist(v.get("filepath"), iframe_url, hdr)
            if not got:
                return None
            link, subs = got
            return {"title": v.get("title") or "Плеер", "hls": link, "mp4": None,
                    "dash": None, "audio": [], "subtitles": subs}

        out = []
        with ThreadPoolExecutor(max_workers=min(6, len(variants) or 1)) as ex:
            for s in ex.map(job, variants):
                if s:
                    out.append(s)
        return out, []

    # сериал: по эпизоду (первая озвучка) -> seasons
    tasks = []
    for e in eps:
        vs = [v for v in (e.get("episodeVariants") or []) if isinstance(v, dict)]
        if not vs:
            continue
        so = (e.get("season") or {}).get("order")
        tasks.append((0 if so is None else so, e, vs[0]))
    if not tasks:
        return [], []

    def job2(t):
        return t[0], t[1], _perep_playlist(t[2].get("filepath"), iframe_url, hdr)

    seas = {}
    order = []
    with ThreadPoolExecutor(max_workers=min(6, len(tasks))) as ex:
        for so, e, got in ex.map(job2, tasks):
            if not got:
                continue
            link, subs = got
            if so not in seas:
                seas[so] = []
                order.append(so)
            num = e.get("order")
            seas[so].append({"episode": str(num if num is not None else len(seas[so])),
                             "dash": None, "hls": link, "mp4": None,
                             "audio": [], "subtitles": subs})
    return [], [{"season": so, "episodes": seas[so]} for so in order if seas.get(so)]


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
            # прямой транспорт предпочтителен (плееры банят прокси-IP), но на
            # Railway может понадобиться прокси — fetch_embed пробует оба
            return fetch_embed(url, lambda u, use_proxy, timeout: http_get(
                u, timeout=timeout, use_proxy=use_proxy), timeout=30)

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
# Новый hdrezka.my закрыт Cloudflare: обычный requests получает 403
# («Just a moment»). Обходим через TLS-имперсонацию curl_cffi.
# Домены эмбед-плееров ротируются (ortified.ws / luxembd.ws / nextembed.ws ...),
# но все отдают блок makePlayer({ source:{hls,dash} }) или seasons:[...] —
# оттуда и забираем прямые потоки в наш кастомный плеер.
try:
    from curl_cffi import requests as _cffi
except Exception:  # curl_cffi не установлен — сайт, скорее всего, не откроется
    _cffi = None

HDREZKA_IMPERSONATE = os.environ.get("HDREZKA_IMPERSONATE", "safari17_0")


# Сессия на поток: keep-alive (без нового TLS-рукопожатия к Cloudflare/прокси
# на каждый запрос) + общие cookies Cloudflare внутри потока.
_hd_local = threading.local()
_hd_direct_local = threading.local()


def _hd_session():
    s = getattr(_hd_local, "session", None)
    if s is None:
        s = _cffi.Session(impersonate=HDREZKA_IMPERSONATE,
                          proxies=make_proxy() or None)
        _hd_local.session = s
    return s


def _hd_direct_session():
    """Сессия без прокси: эмбед-плееры (nextembed и др.) банят прокси-IP (422)."""
    s = getattr(_hd_direct_local, "session", None)
    if s is None:
        s = _cffi.Session(impersonate=HDREZKA_IMPERSONATE)
        _hd_direct_local.session = s
    return s


def _hd_get(url, referer=None, timeout=30, use_proxy=True, headers=None):
    h = {"Referer": referer or HDREZKA_ORIGIN}
    if headers:
        h.update(headers)
    if _cffi is not None:
        s = _hd_session() if use_proxy else _hd_direct_session()
        return _retry_get(lambda: s.get(url, headers=h, timeout=timeout),
                          attempts=3 if use_proxy else 1, delay=0.6)
    return _retry_get(
        lambda: _get_session().get(url, headers={**default_headers, **h},
                                   timeout=timeout, proxies=make_proxy() if use_proxy else None),
        attempts=3 if use_proxy else 1, delay=0.6)


def _hd_post(url, data, referer=None, timeout=30):
    headers = {"Referer": referer or HDREZKA_ORIGIN}
    if _cffi is not None:
        return _hd_session().post(url, data=data, headers=headers, timeout=timeout)
    return _get_session().post(url, headers={**default_headers, **headers},
                               timeout=timeout, proxies=make_proxy())


def _hd_abs(src):
    if not src:
        return None
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return HDREZKA_ORIGIN.rstrip("/") + src
    return src


def _hd_parse_cards(html):
    """Карточки из #dle-content (листинги каталога и результаты поиска)."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select("#dle-content .card_item") or soup.select(".card_item"):
        a = item.select_one("a.card_item__title") or item.select_one("a[href]")
        if not a or not a.get("href"):
            continue
        image = None
        img = item.select_one("img")
        if img:
            image = _hd_abs(img.get("data-src") or img.get("src"))
            if image and image.endswith("net_postera.png"):  # заглушка «нет постера»
                image = None
        year = None
        misc = item.select_one(".card_item__misc")
        if misc:
            m = re.search(r"\b(19|20)\d{2}\b", misc.get_text(" ", strip=True))
            if m:
                year = int(m.group(0))
        out.append({
            "title": a.get_text(" ", strip=True),
            "url": _hd_abs(a["href"]),
            "image": image,
            "rating": None,
            "year": year,
        })
    return out


def hdrezka_search(query, find_all=False):
    """Поиск по новому hdrezka.my (DLE-поиск через index.php)."""
    r = _hd_post(HDREZKA_ORIGIN + "index.php",
                 {"do": "search", "subaction": "search", "story": query, "search_start": 0},
                 referer=HDREZKA_ORIGIN)
    return _hd_parse_cards(r.content)


# Категории каталога hdrezka (xSort фильтрует внутри выбранной категории).
HDREZKA_CATS = {
    "films": "films/",
    "series": "serialy/",
    "cartoons": "multfilmy/",
    "cartoonseries": "multseries/",
    "anime": "anime/",
    "animeseries": "anime-serialy/",
}


def hdrezka_browse(cat="films", genre=None, country=None, year=None, sort=None, page=1):
    """Каталог с фильтрами жанр/год/страна/сортировка (механизм xSort).

    Фильтры копятся в серверной сессии сайта, поэтому на каждый запрос создаём
    отдельную сессию — иначе фильтры разных пользователей перемешаются.
    """
    path = HDREZKA_CATS.get(cat, "films/")
    list_url = HDREZKA_ORIGIN + path
    headers = {"Referer": HDREZKA_ORIGIN}
    if _cffi is not None:
        sess = _cffi.Session(impersonate=HDREZKA_IMPERSONATE, proxies=make_proxy() or None)
    else:
        sess = _get_session()
    try:
        sess.get(list_url, headers=headers, timeout=30)
        for field, value in (("defaultsort", sort), ("genre", genre),
                             ("strana", country), ("year", year)):
            if value:
                sess.post(list_url,
                          data={"xsort": 1, "xs_field": field, "xs_value": value},
                          headers={"Referer": list_url, "X-Requested-With": "XMLHttpRequest"},
                          timeout=30)
        page = max(1, int(page or 1))
        url = list_url if page == 1 else "%s%spage/%d/" % (HDREZKA_ORIGIN, path, page)
        r = sess.get(url, headers=headers, timeout=30)
    finally:
        if _cffi is not None:
            try:
                sess.close()
            except Exception:
                pass
    if r.status_code != 200:
        raise RuntimeError("hdrezka вернула %s" % r.status_code)
    return _hd_parse_cards(r.content)


def hdrezka_filters(cat="films"):
    """Список доступных значений фильтров (жанры/годы/страны/сортировка)."""
    path = HDREZKA_CATS.get(cat, "films/")
    r = _hd_get(HDREZKA_ORIGIN + path, timeout=30)
    soup = BeautifulSoup(r.content, "html.parser")
    out = {"genre": [], "year": [], "country": [], "sort": []}
    fmap = {"genre": "genre", "year": "year", "strana": "country", "defaultsort": "sort"}
    for ul in soup.select(".xsort-ul"):
        key = fmap.get(ul.get("data-field"))
        if not key:
            continue
        for li in ul.select("li"):
            value = (li.get("data-val") or "").strip()
            if value:  # пустое значение = «все», оно не нужно
                out[key].append({"value": value, "label": li.get_text(" ", strip=True)})
    return out


_poster_cache = {}
_poster_lock = threading.Lock()


def hdrezka_poster_hq(url):
    """HQ-обложка фильма из JSON-LD карточки (/uploads/parser/…, ~550x786).

    В листингах отдаются только маленькие превью (/uploads/mini/…), поэтому
    качественный постер достаём со страницы фильма и кэшируем.
    """
    if not url:
        return None
    with _poster_lock:
        if url in _poster_cache:
            return _poster_cache[url]
    hq = None
    try:
        r = _hd_get(url, timeout=25)
        if r.status_code == 200:
            for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
                                 r.text, re.S):
                mm = re.search(r'"image"\s*:\s*\[?\s*"([^"]+)"', m.group(1))
                if mm:
                    hq = _hd_abs(mm.group(1))
                    break
            if not hq:
                og = BeautifulSoup(r.content, "html.parser").find("meta", property="og:image")
                if og and og.get("content"):
                    hq = _hd_abs(og["content"])
    except Exception:
        hq = None
    with _poster_lock:
        if len(_poster_cache) > 8192:
            _poster_cache.clear()
        _poster_cache[url] = hq
    return hq


def _hd_players_from_video_page(html):
    """Вкладки-плееры со страницы /video/<id>/ (трейлер YouTube пропускаем)."""
    soup = BeautifulSoup(html, "html.parser")
    players = []
    for tab in soup.select(".full-article__player__tabs_item"):
        src = tab.get("data-src")
        if not src or "youtube." in src or "youtu.be" in src:
            continue
        players.append({"title": tab.get_text(" ", strip=True) or "Плеер", "url": _hd_abs(src)})
    return players


def hdrezka_film(url):
    """Карточка фильма/сериала нового hdrezka.my + прямые HLS/DASH в наш плеер."""
    r = _hd_get(url)
    if r.status_code != 200:
        raise RuntimeError(f"hdrezka вернула {r.status_code}")
    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.select_one("h1.full-article__title") or soup.select_one("h1")
    name, year, raw_title = None, None, None
    if h1:
        season_span = h1.select_one("span.season")  # у сериалов: «1 сезон 1-25 серия»
        if season_span:
            season_span.extract()
        raw_title = h1.get_text(" ", strip=True)
        m = re.search(r"\((\d{4})\)", raw_title)
        if m:
            year = int(m.group(1))
        name = _clean_title(re.sub(r"\s*\((\d{4})\)\s*$", "", raw_title)) or raw_title

    orig_el = soup.select_one(".full-article__original-title")
    orig_name = orig_el.get_text(" ", strip=True) if orig_el else None

    poster_el = soup.select_one(".full-article__poster img") or soup.select_one("#onlines")
    thumb = _hd_abs(poster_el.get("src")) if poster_el else None
    # HQ-обложка: og:image, иначе JSON-LD (первый обычно /uploads/parser/… — самый крупный)
    thumb_hq = None
    m = re.search(r'<script[^>]*application/ld\+json[^>]*>.*?"image"\s*:\s*\[?\s*"(/[^"]+)"',
                  html, re.S)
    if m:
        thumb_hq = _hd_abs(m.group(1))
    og = soup.find("meta", property="og:image")
    og_hq = _hd_abs(og.get("content")) if og and og.get("content") else None
    if not thumb_hq or ("/uploads/parser/" not in thumb_hq and og_hq):
        thumb_hq = og_hq or thumb_hq
    if not thumb or thumb.endswith("net_postera.png"):
        thumb = thumb_hq or thumb

    desc = None
    d = soup.select_one(".full-article__description")
    if d:
        for tag in d.find_all(["script", "style", "h1", "h2", "h3"]):
            tag.extract()
        desc = re.sub(r"\s+", " ", d.get_text(" ", strip=True)).strip()
    if not desc:
        ogd = soup.find("meta", property="og:description")
        if ogd:
            desc = ogd.get("content")

    rating = None
    imdb = soup.select_one(".full-article__list .imdb b") or soup.select_one(".imdb b")
    if imdb:
        m = re.search(r"\d+(?:[.,]\d+)?", imdb.get_text())
        if m:
            rating = float(m.group(0).replace(",", "."))

    cats = []
    for li in soup.select(".full-article__list li"):
        lab = li.find("span", class_="full-article__list_title")
        if lab and "Категор" in lab.get_text():
            for a in li.find_all("a"):
                v = a.get_text(strip=True)
                if v and "4k" not in v.lower() and v not in cats:
                    cats.append(v)

    # страница с плеерами: /video/<id>/
    video_url = None
    online = soup.select_one("a.full-article__buttons--online")
    if online and online.get("href"):
        video_url = _hd_abs(online["href"])
    if not video_url:
        m = re.search(r'href="(/video/\d+/)"', html)
        if m:
            video_url = _hd_abs(m.group(1))

    players = []
    if video_url:
        vr = _hd_get(video_url, referer=url)
        if vr.status_code == 200:
            players = _hd_players_from_video_page(vr.text)

    # прямые потоки: фильм -> source:{...}, сериал -> seasons:[...]
    sources, seasons = [], []
    if players:
        from concurrent.futures import ThreadPoolExecutor

        def _fetch(p):
            # эмбед-плееры (nextembed и др.) банят прокси-IP (422) — fetch_embed
            # сам подбирает рабочий транспорт и запоминает его по хосту
            return fetch_embed(p["url"], lambda u, use_proxy, timeout: _hd_get(
                u, referer=video_url or url, timeout=timeout, use_proxy=use_proxy),
                timeout=30)

        with ThreadPoolExecutor(max_workers=min(6, len(players))) as ex:
            pages = list(ex.map(_fetch, players))

        for p, ph in zip(players, pages):
            if not ph:
                continue
            if not seasons:
                seasons = _parse_serial_seasons(ph)
            src = _parse_movie_source(ph)
            if src and (src.get("hls") or src.get("mp4") or src.get("dash")):
                src["title"] = p["title"]
                sources.append(src)
            elif "perepolokha." in p["url"]:
                # SPA-плеер: прямых ссылок в HTML нет — берём через его API
                psrc, psea = _parse_perepolokha(p["url"], ph)
                sources.extend(psrc)
                if not seasons and psea:
                    seasons = psea

    if seasons:
        seasons.sort(key=lambda s: s.get("season") or 0)

    is_series = bool(seasons) or any("сериал" in c.lower() for c in cats)
    return {
        "source": "hdrezka",
        "url": url,
        "origin": HDREZKA_ORIGIN,
        "name": name,
        "origName": orig_name,
        "description": desc,
        "releaseYear": year,
        "thumbnail": thumb,
        "thumbnailHQ": thumb_hq or thumb,
        "rating": {"value": rating, "votes": None},
        "type": "video.tv_series" if is_series else "video.movie",
        "category": "category.series" if is_series else "category.film",
        "categories": cats,
        "isSeries": is_series,
        "sources": sources,
        "players": players,
        "translators": {},
        "seasons": seasons,
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
# Главная собирается из hdrezka: он отдаёт HQ-обложки (550×786) и его плеер
# perepolokha работает через прокси, а значит играет и на Railway, где
# датацентровый IP плееры kinogo (ortified) блокируют. Поля: (заголовок, категория, жанр, сортировка).
HOME_ROWS = [
    ("Новинки",     "films",    None,         "date"),
    ("Боевики",     "films",    "боевик",     None),
    ("Комедии",     "films",    "комедия",    None),
    ("Фантастика",  "films",    "фантастика", None),
    ("Ужасы",       "films",    "ужасы",      None),
    ("Драмы",       "films",    "драма",      None),
    ("Триллеры",    "films",    "триллер",    None),
    ("Мультфильмы", "cartoons", None,         None),
    ("Аниме",       "anime",    None,         None),
    ("Сериалы",     "series",   None,         None),
]


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


@app.route("/api/home")
def api_home():
    cached = cache_get("home", 1800)
    if cached is not None:
        return json_cached({"ok": True, "rows": cached}, 600)

    from concurrent.futures import ThreadPoolExecutor
    jobs = [(title, (lambda c=cat, g=genre, s=sort:
                     hdrezka_browse(c, genre=g, sort=s)))
            for title, cat, genre, sort in HOME_ROWS]

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
    return json_cached({"ok": True, "rows": rows}, 600)


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
        "defaultSource": "hdrezka",
        "proxy": bool(PROXY),
        "videoProxy": bool(VIDEO_PROXY),
        "embedTransport": EMBED_TRANSPORT,
        "hdrezkaAuthorized": bool(get_hdrezka_cookies()),
    })


@app.route("/api/diag")
def api_diag():
    """Диагностика транспорта с IP самого сервера (нужна для Railway).

    Открой /api/diag — покажет исходящий IP и что реально работает:
    сайт источника и страницы плееров напрямую / через прокси.
    С /api/diag?url=<страница фильма> проверит конкретный фильм по шагам.
    """
    def one(fn, url):
        try:
            r = fn(url)
            return {"status": r.status_code, "bytes": len(r.content)}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    out = {
        "ok": True,
        "egressIp": None,
        "proxy": bool(PROXY),
        "videoProxy": bool(VIDEO_PROXY),
        "embedTransport": EMBED_TRANSPORT,
        "embedMode": dict(_embed_mode),
        "checks": {},
    }
    try:
        out["egressIp"] = requests.get("https://api.ipify.org", timeout=10).text
    except Exception as e:
        out["egressIp"] = f"err: {e}"

    for label, u in (("kinogo.sarl", KINOGO_ORIGIN + "/"),
                     ("hdrezka.my", HDREZKA_ORIGIN)):
        out["checks"][label] = {
            "direct": one(lambda x: http_get(x, use_proxy=False), u),
            "proxy": one(lambda x: http_get(x, use_proxy=True), u),
        }

    film_url = (request.args.get("url") or "").strip()
    if film_url:
        steps = out.setdefault("film", {})
        for label, fn in (("direct", lambda x: http_get(x, use_proxy=False)),
                          ("proxy", lambda x: http_get(x, use_proxy=True))):
            r = one(fn, film_url)
            steps.setdefault("page", {})[label] = r

        players = []
        try:
            r = http_get(film_url, use_proxy=False)
            if r.status_code != 200:
                r = http_get(film_url, use_proxy=True)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, "html.parser")
                players = [abs_url(i.get("src")) for i in soup.find_all("iframe")
                           if i.get("src") and "http" in i.get("src")]
        except Exception as e:
            steps["pageError"] = f"{type(e).__name__}: {e}"

        steps["players"] = []
        for pu in players:
            entry = {"url": pu, "direct": None, "proxy": None}
            for label, use_proxy in (("direct", False), ("proxy", True)):
                try:
                    pr = http_get(pu, use_proxy=use_proxy, timeout=30) if detect_source(
                        film_url) == "kinogo" else _hd_get(pu, use_proxy=use_proxy)
                    src = _parse_movie_source(pr.text) if pr.status_code == 200 else None
                    entry[label] = {
                        "status": pr.status_code,
                        "bytes": len(pr.content),
                        "parsed": bool(src and (src.get("hls") or src.get("dash") or src.get("mp4"))),
                    }
                except Exception as e:
                    entry[label] = {"error": f"{type(e).__name__}: {e}"}
            steps["players"].append(entry)

    out["videoDirectMode"] = VIDEO_DIRECT

    # проверка самого видеопотока: пустит ли этот сервер видео напрямую
    stream_url = (request.args.get("stream") or "").strip()
    if not stream_url and film_url:
        try:
            film = cache_get(f"film:{film_url}", 600) or (
                kinogo_film(film_url) if detect_source(film_url) == "kinogo"
                else hdrezka_film(film_url))
            for s in (film.get("sources") or []):
                if s.get("hls"):
                    stream_url = s["hls"]
                    break
        except Exception as e:
            out["streamResolveError"] = f"{type(e).__name__}: {e}"

    if stream_url:
        def _probe(u, proxied):
            entry = {"ok": False, "ms": None, "status": None, "type": "", "bytes": 0, "error": None}
            t0 = time.time()
            try:
                if _cffi is not None:
                    sess = _cffi.Session(impersonate=HDREZKA_IMPERSONATE,
                                         proxies=(make_video_proxy() or None) if proxied else None)
                    h = dict(default_headers)
                    h.pop("User-Agent", None)
                    r = sess.get(u, headers=h, stream=True, timeout=12)
                else:
                    r = _get_session().get(u, headers=default_headers, stream=True, timeout=12,
                                           proxies=make_video_proxy() if proxied else {})
                entry["status"] = r.status_code
                entry["type"] = r.headers.get("Content-Type", "")
                body = b"".join(r.iter_content(65536))
                entry["bytes"] = len(body)
                entry["ok"] = (r.status_code == 200 and entry["bytes"] > 0
                               and "text/html" not in entry["type"].lower())
                r.close()
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
            entry["ms"] = int((time.time() - t0) * 1000)
            return entry

        check = {"url": stream_url, "master": {}, "variant": {}}
        for label, proxied in (("direct", False), ("proxy", True)):
            if proxied and not VIDEO_PROXY:
                continue
            check["master"][label] = _probe(stream_url, proxied)

        # берём рабочий master и проверяем первый вариант (это самый тяжёлый файл)
        for label, proxied in (("direct", False), ("proxy", True)):
            if check["master"].get(label, {}).get("ok"):
                try:
                    if _cffi is not None:
                        sess = _cffi.Session(impersonate=HDREZKA_IMPERSONATE,
                                             proxies=(make_video_proxy() or None) if proxied else None)
                        h = dict(default_headers)
                        h.pop("User-Agent", None)
                        txt = sess.get(stream_url, headers=h, timeout=12).text
                    else:
                        txt = _get_session().get(stream_url, headers=default_headers, timeout=12,
                                                 proxies=make_video_proxy() if proxied else {}).text
                    for line in txt.splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            var = urljoin(stream_url, line)
                            for l2, p2 in (("direct", False), ("proxy", True)):
                                if p2 and not VIDEO_PROXY:
                                    continue
                                check["variant"][l2] = _probe(var, p2)
                            break
                except Exception as e:
                    check["variantError"] = f"{type(e).__name__}: {e}"
                break

        check["directUsable"] = bool(check["master"].get("direct", {}).get("ok"))
        out["stream"] = check

    return jsonify(out)


@app.route("/api/search")
def api_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return json_error("Пустой запрос", 400)
    source = request.args.get("source") or "hdrezka"
    find_all = request.args.get("all") in ("1", "true", "yes")

    key = f"search:{source}:{query}:{find_all}"
    cached = cache_get(key, 300)
    if cached is not None:
        return json_cached({"ok": True, "results": cached, "source": source}, 300)

    try:
        if source == "kinogo":
            results = kinogo_search(query)
        else:
            results = hdrezka_search(query, find_all)
            if not results:  # hdrezka ничего не нашла — не оставляем пустой экран
                results = kinogo_search(query)
        cache_set(key, results)
        return json_cached({"ok": True, "results": results, "source": source}, 300)
    except Exception as e:
        return json_error(f"{type(e).__name__}: {e}")


@app.route("/api/filters")
def api_filters():
    """Доступные значения фильтров каталога (жанр/год/страна/сортировка)."""
    cat = request.args.get("cat") or "films"
    key = f"filters:{cat}"
    cached = cache_get(key, 21600)
    if cached is None:
        try:
            cached = hdrezka_filters(cat)
            cache_set(key, cached)
        except Exception as e:
            return json_error(f"{type(e).__name__}: {e}")
    return json_cached({"ok": True, "filters": cached}, 21600)


@app.route("/api/browse")
def api_browse():
    """Каталог с фильтрами: /api/browse?cat=films&genre=боевик&year=2024&page=1"""
    cat = request.args.get("cat") or "films"
    genre = request.args.get("genre") or None
    country = request.args.get("country") or None
    year = request.args.get("year") or None
    sort = request.args.get("sort") or None
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        page = 1

    key = f"browse:{cat}:{genre}:{country}:{year}:{sort}:{page}"
    cached = cache_get(key, 900)
    if cached is not None:
        return json_cached({"ok": True, "items": cached, "page": page}, 600)

    try:
        items = hdrezka_browse(cat, genre, country, year, sort, page)
        cache_set(key, items)
        return json_cached({"ok": True, "items": items, "page": page}, 600)
    except Exception as e:
        return json_error(f"{type(e).__name__}: {e}")


@app.route("/api/posters")
def api_posters():
    """Пакетное получение HQ-обложек по ссылкам на фильмы (в листингах только превью)."""
    raw = request.args.get("urls") or ""
    urls = [u for u in (x.strip() for x in raw.split(",")) if u][:24]
    if not urls:
        return json_error("Не переданы urls", 400)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(6, len(urls))) as ex:
        posters = dict(zip(urls, ex.map(hdrezka_poster_hq, urls)))
    return json_cached({"ok": True, "posters": posters}, 86400)


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
            film = cache_get(f"film:{url}", 600) or hdrezka_film(url)
            if not film.get("sources"):
                return json_error("Прямой поток не найден")
            data = {"kind": "hls", "origin": HDREZKA_ORIGIN, "sources": film["sources"]}
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
    hint_transport(target, request.args.get("src"))
    try:
        r = video_get(target, headers=default_headers, timeout=30)
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
    hint_transport(target, request.args.get("src"))

    headers = dict(default_headers)
    referer = request.args.get("ref")
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = referer.rstrip("/")
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    # Держим не больше PROXY_CONN_LIMIT одновременных запросов к прокси: телефон
    # открывает пачку соединений, а провайдер на лимит отвечает 429.
    slot = _proxy_slots
    if slot is not None and not slot.acquire(timeout=25):
        return json_error("Прокси занят, попробуйте ещё раз", 503)
    released = [slot is None]

    def release():
        if not released[0]:
            released[0] = True
            slot.release()

    try:
        upstream = video_get(target, headers=headers, stream=True, timeout=30)
    except Exception as e:
        release()
        return json_error(f"{type(e).__name__}: {e}")

    # Ошибку прокси не отдаём плееру как «видео» — иначе чёрный экран/поломка перемотки.
    if upstream.status_code in _RETRY_CODES:
        code = upstream.status_code
        upstream.close()
        release()
        return json_error(f"Источник/прокси перегружен ({code})", 503)

    ctype = upstream.headers.get("Content-Type", "").lower()
    it = upstream.iter_content(chunk_size=64 * 1024)
    try:
        first = next(it)
    except StopIteration:
        first = b""

    # плейлист HLS -> переписываем все ссылки на прокси
    if "mpegurl" in ctype or first[:7] == b"#EXTM3U":
        body = _rewrite_m3u8((first + b"".join(it)).decode("utf-8", "ignore"), target)
        upstream.close()
        release()
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
            release()

    resp = Response(stream_with_context(gen()), status=upstream.status_code, headers=resp_headers)
    resp.call_on_close(release)  # подстраховка, если тело так и не поехало
    return resp


@app.route("/api/subtitle")
def api_subtitle():
    target = request.args.get("url")
    if not target:
        return json_error("Не передан url", 400)
    try:
        r = video_get(target, headers=default_headers, timeout=30)
        return Response(r.content, status=r.status_code,
                        headers={"Content-Type": "text/vtt; charset=utf-8", "Cache-Control": "no-store"})
    except Exception as e:
        return json_error(f"{type(e).__name__}: {e}")


_img_cache = {}
_img_lock = threading.Lock()


def _make_thumb(data, w, h):
    """Уменьшает постер до w×h (не растягивая) и отдаёт компактный WebP."""
    if Image is None or not w or not h:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGB")
        im.thumbnail((w, h), Image.LANCZOS)  # только уменьшение, без апскейла
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=85, method=4)
        return buf.getvalue()
    except Exception:
        return None


@app.route("/api/img")
def api_img():
    """Прокси постеров hdrezka: сайт за Cloudflare, браузер получает 403.

    Параметры w/h включают серверный ресайз (Retina-резкость + меньше трафика).
    """
    target = request.args.get("url")
    if not target:
        return json_error("Не передан url", 400)
    try:
        w = min(1600, int(request.args.get("w") or 0))
        h = min(2400, int(request.args.get("h") or 0))
    except (TypeError, ValueError):
        w = h = 0

    key = (target, w, h)
    if Image is not None and w and h:
        with _img_lock:
            cached = _img_cache.get(key)
        if cached is not None:
            return Response(cached, content_type="image/webp",
                            headers={"Cache-Control": "public, max-age=604800"})

    try:
        r = _hd_get(target, timeout=25)
    except Exception as e:
        return json_error(f"{type(e).__name__}: {e}")
    if r.status_code != 200:
        return ("", r.status_code)

    data = r.content
    ctype = r.headers.get("Content-Type", "image/jpeg")
    thumb = _make_thumb(data, w, h)
    if thumb is not None:
        data, ctype = thumb, "image/webp"
        with _img_lock:
            if len(_img_cache) > 512:  # простая защита от роста памяти
                _img_cache.clear()
            _img_cache[key] = data
    return Response(data, content_type=ctype,
                    headers={"Cache-Control": "public, max-age=604800"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"Сервер: http://{host}:{port}/")
    print(f"Киного: {KINOGO_ORIGIN}   HDRezka: {HDREZKA_ORIGIN}")
    app.run(host=host, port=port, threaded=True)
