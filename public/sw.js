const CACHE = 'hot-gap-v6';
const SHELL = [
  './', './manifest.webmanifest?v=2', './favicon-32.png?v=2', './apple-touch-icon.png?v=2',
  './icon-192.png?v=2', './icon-512.png?v=2', './icon-maskable-512.png?v=2',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))));
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const isData = new URL(event.request.url).pathname.includes('/data/');
  if (isData) {
    // Authenticated feeds must never survive logout in the service-worker cache.
    event.respondWith(fetch(event.request, { cache: 'no-store' }));
    return;
  }
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});
