// Ridea Service Worker — cache shell for offline UI
const CACHE = 'ridea-v1';
const SHELL = ['/recorder', '/static/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  // Network first for API calls
  if (e.request.url.includes('/api/')) return;
  // Cache first for shell
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
