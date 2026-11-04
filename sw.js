/* Кино-88 — service worker.
   Стратегия: network-first (чтобы не было устаревшего контента), кэш только
   как офлайн-запасная копия для главной страницы. API не кэшируем. */
const CACHE = "k88-v1";

self.addEventListener("install", e => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(["/", "/manifest.json"]).catch(() => {}))
  );
});

self.addEventListener("activate", e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const u = new URL(req.url);
  if (u.origin !== location.origin) return;
  if (u.pathname.startsWith("/api/")) return;
  if (u.pathname.startsWith("/sw.js") || u.pathname.startsWith("/manifest")) return;

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
