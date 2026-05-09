const CACHE = 'narrator-v1';
const SHELL = ['/', '/static/style.css', '/static/manifest.json', '/static/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Never cache audio or generate POSTs — always network
  if (url.pathname.startsWith('/audio/') || url.pathname === '/generate') {
    return;
  }
  // Cache-first for shell assets
  if (e.request.method === 'GET' && SHELL.includes(url.pathname)) {
    e.respondWith(
      caches.match(e.request).then((r) => r || fetch(e.request))
    );
  }
});
