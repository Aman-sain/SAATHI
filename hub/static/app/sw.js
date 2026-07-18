/* SAATHI PWA service worker (§7): caches the STATIC SHELL ONLY.
 * API responses and WS traffic are never cached — live data must be live. */
"use strict";

const CACHE = "saathi-shell-v4"; // bump on ANY shell-file change or phones keep the old shell
const SHELL = ["./", "index.html", "app.js", "style.css", "manifest.json", "icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // shell files only — never /api/* (and WS upgrades never hit fetch)
  if (url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request))
  );
});
