// Assets versionados do collectstatic dispensam nova transferência pela rede.
// HTML contém CSRF/sessão: deve sempre vir do servidor.
const CACHE = "mr-blake-v2";

self.addEventListener("install", (e) => {
  e.waitUntil(self.skipWaiting());
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith("mr-blake-") && k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin || !url.pathname.startsWith("/static/")) return;
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const cached = await cache.match(e.request);
    if (cached && /\.[a-f0-9]{12}\./.test(url.pathname)) return cached;
    try {
      const response = await fetch(e.request);
      if (response.ok) {
        const copy = response.clone();
        e.waitUntil((async () => {
          await cache.put(e.request, copy);
          const keys = await cache.keys();
          await Promise.all(keys.slice(0, Math.max(0, keys.length - 64)).map((key) => cache.delete(key)));
        })());
      }
      return response;
    } catch (error) {
      if (cached) return cached;
      throw error;
    }
  })());
});
