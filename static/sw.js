const STATIC_CACHE = 'licensestatus-static-v2';
const API_CACHE = 'licensestatus-api-v2';
const PRECACHE = [
    '/',
    '/manifest.json',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
];

self.addEventListener('install', e => {
    e.waitUntil(
        caches.open(STATIC_CACHE).then(c => c.addAll(PRECACHE))
    );
    self.skipWaiting();
});

self.addEventListener('activate', e => {
    const KEEP = new Set([STATIC_CACHE, API_CACHE]);
    e.waitUntil(
        caches.keys()
            .then(keys => Promise.all(
                keys.filter(k => !KEEP.has(k)).map(k => caches.delete(k))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', e => {
    const req = e.request;
    if (req.method !== 'GET') return;
    const url = new URL(req.url);

    // /api/check — installed PWA must keep working offline for previously
    // searched licenses. Network-first so results stay fresh; on success
    // we mirror found:true responses into a cache keyed by URL (the URL
    // already contains the license number); on failure we replay the cache.
    if (url.pathname === '/api/check') {
        e.respondWith(handleCheck(req));
        return;
    }

    // Other API (stats, offices, etc.): passthrough with offline stub on failure.
    if (url.pathname.startsWith('/api/')) {
        e.respondWith(
            fetch(req).catch(() =>
                new Response(JSON.stringify({ found: false, offline: true }), {
                    headers: { 'Content-Type': 'application/json' }
                })
            )
        );
        return;
    }

    // Navigations / HTML — network-first so config changes (theme, captcha
    // key) propagate without forcing a cache bust. Falls back to cached
    // shell when offline.
    const accept = req.headers.get('accept') || '';
    if (req.mode === 'navigate' || accept.includes('text/html')) {
        e.respondWith(
            fetch(req)
                .then(res => {
                    const clone = res.clone();
                    caches.open(STATIC_CACHE).then(c => c.put(req, clone));
                    return res;
                })
                .catch(() => caches.match(req).then(m => m || caches.match('/')))
        );
        return;
    }

    // Static assets — cache-first.
    e.respondWith(
        caches.match(req).then(cached =>
            cached || fetch(req).then(res => {
                const clone = res.clone();
                caches.open(STATIC_CACHE).then(c => c.put(req, clone));
                return res;
            })
        ).catch(() => caches.match('/'))
    );
});

async function handleCheck(request) {
    const cache = await caches.open(API_CACHE);
    try {
        const fresh = await fetch(request);
        if (fresh && fresh.ok) {
            // Only mirror successful matches. Don't cache "not found" or 403
            // captcha rejections — they may flip on the next sync / next try.
            try {
                const body = await fresh.clone().json();
                if (body && body.found === true) {
                    cache.put(request, fresh.clone());
                }
            } catch (_) { /* not JSON — skip caching */ }
        }
        return fresh;
    } catch (_) {
        const cached = await cache.match(request);
        if (cached) {
            // Re-emit the cached response with X-Cache-Hit so the page can
            // surface the "showing cached result" badge.
            const body = await cached.clone().text();
            const headers = new Headers(cached.headers);
            headers.set('X-Cache-Hit', 'true');
            return new Response(body, {
                status: cached.status,
                statusText: cached.statusText,
                headers,
            });
        }
        return new Response(
            JSON.stringify({ found: false, offline: true }),
            { headers: { 'Content-Type': 'application/json' } }
        );
    }
}
