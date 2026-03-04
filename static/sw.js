// Ridea Service Worker — versioned cache, network-first API, SW_UPDATED notify
const CACHE = 'ridea-v1.0.14';
const SHELL = ['/recorder', '/static/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => {
      // Notify all open clients that SW was updated
      return self.clients.matchAll({ includeUncontrolled: true }).then(clients => {
        clients.forEach(c => c.postMessage({ type: 'SW_UPDATED' }));
      });
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  // Network first for API calls
  if (e.request.url.includes('/api/')) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }
  // Cache first for static shell
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
