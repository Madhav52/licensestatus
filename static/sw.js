/* ──────────────────────────────────────────────────────────────────
   LicenseStatus Nepal — Service Worker
   ──────────────────────────────────────────────────────────────────
   Offline strategy summary:

     /                       network-first → cache (HTML shell)
     /manifest.json          cache-first   → network
     /static/icons/*         cache-first   → network
     /static/*               cache-first   → network
     fonts.googleapis.com    stale-while-revalidate (CSS)
     fonts.gstatic.com       cache-first   (font binaries)
     /api/stats              stale-while-revalidate
     /api/offices            stale-while-revalidate
     /api/last-updated       network-only  (version probe, must be live)
     /api/licenses           stale-while-revalidate (with ETag)
     /api/check              network-first → cache (per-license, only
                             caches found:true responses; surfaces
                             X-Cache-Hit: true on offline replay)
     anything else GET       network → cache fallback

   reCAPTCHA: never cached. When the device is fully offline the request
   never reaches the server, so the server-side captcha gate is not
   invoked. When the device is online but grecaptcha is blocked, the
   page falls back to the IndexedDB lookup it builds via /api/licenses.
   ────────────────────────────────────────────────────────────────── */

const SW_VERSION   = 'v4';
const STATIC_CACHE = `licensestatus-static-${SW_VERSION}`;
const API_CACHE    = `licensestatus-api-${SW_VERSION}`;
const FONT_CACHE   = `licensestatus-fonts-${SW_VERSION}`;

const PRECACHE = [
    '/',
    '/manifest.json',
    '/static/icons/icon-192.png',
];

self.addEventListener('install', e => {
    e.waitUntil(
        caches.open(STATIC_CACHE).then(c =>
            // Use individual put() so a single missing asset doesn't tank
            // the whole install (e.g. icon-512 is referenced by manifest
            // but not always present on disk).
            Promise.all(PRECACHE.map(url =>
                fetch(url, { cache: 'reload' })
                    .then(r => r.ok ? c.put(url, r) : null)
                    .catch(() => null)
            ))
        ).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', e => {
    const KEEP = new Set([STATIC_CACHE, API_CACHE, FONT_CACHE]);
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

    // ── Google Fonts CSS — small, mutable. Stale-while-revalidate.
    if (url.hostname === 'fonts.googleapis.com') {
        e.respondWith(staleWhileRevalidate(req, FONT_CACHE));
        return;
    }
    // ── Google Fonts binaries — large, immutable. Cache-first.
    if (url.hostname === 'fonts.gstatic.com') {
        e.respondWith(cacheFirst(req, FONT_CACHE));
        return;
    }

    // ── reCAPTCHA — never cache, never intercept. Let it fail naturally
    //    when offline; the page handles that path by falling back to the
    //    local IndexedDB lookup.
    const isRecaptcha =
        (url.hostname === 'www.google.com'  && url.pathname.startsWith('/recaptcha')) ||
        (url.hostname === 'www.gstatic.com' && url.pathname.includes('/recaptcha'));
    if (isRecaptcha) return;

    // ── Same-origin handling
    if (url.origin === self.location.origin) {

        // /api/check — per-license result; cache successes for offline replay.
        if (url.pathname === '/api/check') {
            e.respondWith(handleCheck(req));
            return;
        }

        // /api/last-updated — must be live (it's the version probe).
        if (url.pathname === '/api/last-updated') {
            e.respondWith(
                fetch(req).catch(() => new Response(
                    JSON.stringify({ last_updated: null, offline: true }),
                    { headers: { 'Content-Type': 'application/json' } }
                ))
            );
            return;
        }

        // /api/licenses — bulk sync; ETag-revalidated, kept warm in cache.
        if (url.pathname === '/api/licenses') {
            e.respondWith(staleWhileRevalidate(req, API_CACHE));
            return;
        }

        // /api/stats and /api/offices — feed UI chrome; safe to be slightly stale.
        if (url.pathname === '/api/stats' || url.pathname === '/api/offices') {
            e.respondWith(staleWhileRevalidate(req, API_CACHE));
            return;
        }

        // Other /api/* — passthrough with offline JSON stub on failure.
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

        // HTML / navigations — network-first so config + template changes
        // propagate; cached shell answers when offline.
        const accept = req.headers.get('accept') || '';
        if (req.mode === 'navigate' || accept.includes('text/html')) {
            e.respondWith(
                fetch(req)
                    .then(res => {
                        if (res && res.ok) {
                            const clone = res.clone();
                            caches.open(STATIC_CACHE).then(c => c.put(req, clone));
                        }
                        return res;
                    })
                    .catch(() =>
                        caches.match(req).then(m => m || caches.match('/'))
                    )
            );
            return;
        }

        // Static assets — cache-first.
        e.respondWith(cacheFirst(req, STATIC_CACHE));
        return;
    }

    // ── Cross-origin (non-font) — passthrough; never block on cache.
    e.respondWith(fetch(req).catch(() => caches.match(req)));
});

/* ───── strategies ───── */

async function cacheFirst(req, cacheName) {
    const cache  = await caches.open(cacheName);
    const cached = await cache.match(req);
    if (cached) return cached;
    try {
        const fresh = await fetch(req);
        if (fresh && fresh.ok) cache.put(req, fresh.clone());
        return fresh;
    } catch (_) {
        // No cached copy and no network — try the shell as a last resort.
        return caches.match('/') || new Response('', { status: 504 });
    }
}

async function staleWhileRevalidate(req, cacheName) {
    const cache  = await caches.open(cacheName);
    const cached = await cache.match(req);

    // Conditional revalidation: if we have a cached response with an ETag
    // we can spend almost nothing to confirm it's still current.
    const headers = new Headers(req.headers);
    if (cached) {
        const tag = cached.headers.get('ETag');
        if (tag) headers.set('If-None-Match', tag);
    }
    const revalReq = new Request(req, { headers });

    const network = fetch(revalReq).then(res => {
        if (!res) return cached || offlineJson();
        if (res.status === 304 && cached) return cached;
        if (res.ok) cache.put(req, res.clone());
        return res;
    }).catch(() => cached || offlineJson());

    // If we have something cached, return it immediately. Otherwise wait
    // for the network attempt (which will either succeed or fall back).
    return cached || network;
}

function offlineJson() {
    return new Response(
        JSON.stringify({ offline: true }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
    );
}

async function handleCheck(request) {
    const cache = await caches.open(API_CACHE);
    try {
        const fresh = await fetch(request);
        if (fresh && fresh.ok) {
            // Only mirror successful matches. Don't cache "not found" or
            // 403 captcha rejections — they may flip on the next sync.
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
            // Re-emit cached response with X-Cache-Hit so the page can
            // surface the "showing cached result" badge.
            const body    = await cached.clone().text();
            const headers = new Headers(cached.headers);
            headers.set('X-Cache-Hit', 'true');
            return new Response(body, {
                status:     cached.status,
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
