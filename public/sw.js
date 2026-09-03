const CACHE = 'hot-gap-v7';
const SHELL = [
  './', './manifest.webmanifest?v=2', './favicon-32.png?v=2', './apple-touch-icon.png?v=2',
  './icon-192.png?v=2', './icon-512.png?v=2', './icon-maskable-512.png?v=2',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))));
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);
  const isPrivateRequest = url.pathname.includes('/data/') || url.pathname.startsWith('/api/');
  if (isPrivateRequest) {
    // Authenticated feeds and APIs must never survive logout in the cache.
    event.respondWith(fetch(event.request, { cache: 'no-store' }));
    return;
  }

  if (event.request.mode === 'navigate') {
    // Vite asset names change on every build. Always fetch the matching HTML
    // first, then refresh the offline fallback so an old shell cannot point at
    // assets removed by rsync --delete.
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (response.ok && url.origin === self.location.origin) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put('./', copy));
          }
          return response;
        })
        .catch(() => caches.match('./')),
    );
    return;
  }

  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});
