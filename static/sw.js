// Bump CACHE whenever this file changes — old SW will clear stale caches
// on activate. App code itself is network-first, so deploys don't need
// a bump just to push new HTML/JS.
const CACHE = 'narrator-v5';

// Assets safe to precache aggressively (they rarely change; when they
// do we'll bump CACHE above).
const SHELL = ['/static/style.css', '/static/manifest.json', '/static/icon.svg'];

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
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);

  // Never touch audio or app API routes — straight to network.
  if (url.pathname.startsWith('/audio/') ||
      url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/images/')) {
    return;
  }

  // HTML pages and login → network-first. Falls back to cache only
  // when offline. This is what kills the "hard refresh required"
  // problem — the browser always sees the latest deployed HTML.
  if (url.pathname === '/' || url.pathname === '/login' ||
      e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // Static shell assets → cache-first.
  if (SHELL.includes(url.pathname)) {
    e.respondWith(
      caches.match(e.request).then((r) => r || fetch(e.request))
    );
  }
});
