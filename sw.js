/* Кино-88 — service worker.
   Стратегии:
   - страницы: network-first (свежий контент), кэш как офлайн-запас;
   - /api/film: network-first — карточка фильма всегда свежая, кэш только
     как офлайн-запас (устаревшее «нет потока» вводить в заблуждение нельзя);
   - каталог/поиск/фильтры/постеры: stale-while-revalidate — открытие берётся
     из кэша мгновенно, обновление прилетает в фоне. */
const CACHE = "k88-v3";
const API_CACHE = "k88-api-v2";
const API_SWR = ["/api/home", "/api/browse", "/api/search", "/api/filters", "/api/posters"];
const API_CACHE_MAX = 300;     // ограничиваем рост кэша API

self.addEventListener("install", e => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(["/", "/manifest.json"]).catch(() => {}))
  );
});

self.addEventListener("activate", e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE && k !== API_CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

async function put(cache, req, res) {
  try {
    await cache.put(req, res.clone());
    const keys = await cache.keys();
    if (keys.length > API_CACHE_MAX) await cache.delete(keys[0]);
  } catch (e) {}
}

async function networkFirst(req) {
  const cache = await caches.open(API_CACHE);
  try {
    const r = await fetch(req);
    if (r && r.ok) put(cache, req, r);
    return r;
  } catch (err) {
    const hit = await cache.match(req);
    if (hit) return hit;
    throw err;
  }
}

async function staleWhileRevalidate(req) {
  const cache = await caches.open(API_CACHE);
  const hit = await cache.match(req);
  const net = fetch(req)
    .then(r => { if (r && r.ok) put(cache, req, r); return r; })
    .catch(() => null);
  if (hit) return hit;                 // мгновенно из кэша, сеть обновит в фоне
  const fresh = await net;
  if (fresh) return fresh;
  return new Response('{"ok":false,"error":"offline"}',
    { status: 503, headers: { "Content-Type": "application/json" } });
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const u = new URL(req.url);
  if (u.origin !== location.origin) return;
  if (u.pathname.startsWith("/sw.js") || u.pathname.startsWith("/manifest")) return;

  if (u.pathname.startsWith("/api/")) {
    if (u.pathname === "/api/film") e.respondWith(networkFirst(req));
    else if (API_SWR.includes(u.pathname)) e.respondWith(staleWhileRevalidate(req));
    return;                            // остальные API — напрямую
  }

  e.respondWith((async () => {
    try {
      const r = await fetch(req);
      if (r && r.ok && (u.pathname === "/" || u.pathname === "/index.html")) {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
      }
      return r;
    } catch (err) {
      const hit = await caches.match(req);
      if (hit) return hit;
      throw err;
    }
  })());
});
