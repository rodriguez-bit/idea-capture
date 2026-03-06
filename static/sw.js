const CACHE_NAME = 'ridea-v3.4.0';
const urlsToCache = ['/recorder', '/static/recorder.html', '/static/icon-192.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(urlsToCache)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))).then(() => { self.clients.matchAll().then(cls => cls.forEach(c => c.postMessage({type:'SW_UPDATED'}))); return self.clients.claim(); })); });
self.addEventListener('fetch', e => { e.respondWith(fetch(e.request).then(r => { if (r.ok && e.request.method === 'GET') { const rc = r.clone(); caches.open(CACHE_NAME).then(c => c.put(e.request, rc)); } return r; }).catch(() => caches.match(e.request))); });
