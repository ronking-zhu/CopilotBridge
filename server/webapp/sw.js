// Minimal service worker: cache the app shell, never cache API calls.
const CACHE = 'copilot-bridge-v22';
const SHELL = ['/', '/index.html', '/icon.svg', '/manifest.webmanifest', '/vendor/msal-browser.min.js'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/')) return; // always go to network for the API

  // Navigations — including the Microsoft sign-in redirect back to "/#code=..." —
  // are served straight from the cached app shell. This keeps the browser from
  // round-tripping to the Dev Tunnel, whose one-time anti-phishing interstitial
  // would otherwise interrupt the OAuth redirect on phones (=> AADSTS900561). The
  // auth code rides in the URL fragment, which the browser preserves, so MSAL
  // still processes it after the cached page loads.
  if (e.request.mode === 'navigate') {
    e.respondWith(
      caches.match('/index.html').then((r) => r || caches.match('/')).then((r) => r || fetch(e.request))
    );
    return;
  }

  e.respondWith(caches.match(e.request).then((r) => r || fetch(e.request)));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || '/?dashboard=1';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (clients) => {
      for (const client of clients) {
        if ('navigate' in client) await client.navigate(target);
        if ('focus' in client) return client.focus();
      }
      return self.clients.openWindow ? self.clients.openWindow(target) : undefined;
    })
  );
});

