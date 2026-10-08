// Lattice's service worker: keeps the page (and d3) so the app opens without a network. The data isn't here:
// the page keeps its own copy of the lattice in IndexedDB and syncs it from /api/snapshot.
const CACHE = "lattice-shell-v1";
const SHELL = ["/", "/manifest.webmanifest", "/apple-touch-icon.png", "/icon-192.png",
               "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"];

self.addEventListener("install", e => {
  // Best effort: a shell that fails to cache (logged out, offline) is cached on the next visit instead.
  e.waitUntil(caches.open(CACHE).then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => {}))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  if (e.request.mode === "navigate" && url.origin === location.origin && url.pathname === "/") {
    // The page: network first, so a new version shows at once; the kept copy when offline. A redirect to the
    // login page is never kept.
    e.respondWith(fetch(e.request).then(r => {
      if (r.ok && !r.redirected) { const copy = r.clone(); caches.open(CACHE).then(c => c.put("/", copy)); }
      return r;
    }).catch(() => caches.match("/").then(r => r || Response.error())));
    return;
  }
  if (SHELL.includes(url.href) || (url.origin === location.origin && SHELL.includes(url.pathname) && url.pathname !== "/")) {
    e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
  }
  // Everything else, the API above all, goes to the network untouched.
});
