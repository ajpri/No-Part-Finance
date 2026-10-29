/* Shared worker for push and the narrowly scoped read-only offline shell. */
const applicationVersion = "__APPLICATION_VERSION__";
const offlineEnabled = "__PWA_OFFLINE_ENABLED__" === "true";
const pushEnabled = "__PUSH_NOTIFICATIONS_ENABLED__" === "true";
self.addEventListener("push", (event) => {
  if (!pushEnabled) return;
  event.waitUntil((async () => {
    const message = event.data?.json();
    const invitation = message?.type === "ledger-invitation";
    if (invitation ? !Number.isSafeInteger(message.invitationId) : !message || typeof message.ledgerId !== "string" || !Number.isSafeInteger(message.proposalId)) return;
    // An invite does not grant access to its ledger; open the recipient's existing Pending view.
    const url = invitation ? "/pending" : `/pending?ledger=${encodeURIComponent(message.ledgerId)}`;
    // Web Push must produce a visible notification (including Safari/iOS).
    // A stable tag coalesces arrivals, and payloads contain no financial details.
    await self.registration.showNotification(invitation ? "Ledger Invitation" : "Pending Review", {body: invitation ? "You’ve been invited to join a ledger. Tap to review." : "A new pending item is ready to review. Tap to open.", tag: invitation ? "ledger-invitation" : `pending:${message.ledgerId}`, data: {url}, icon: "/assets/icon-192.png"});
    try {
      const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
      for (const client of windows) if (client.visibilityState === "visible" && (invitation || new URL(client.url).searchParams.get("ledger") === message.ledgerId)) client.postMessage(invitation ? {type: "invitation-arrival"} : {type: "pending-arrival", ledgerId: message.ledgerId});
    } catch { /* Updating an open page must not prevent the system notification. */ }
  })());
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const candidate = new URL(event.notification.data?.url || "/", self.location.origin);
  if (candidate.origin !== self.location.origin || !["/pending", "/"].includes(candidate.pathname)) return;
  // A fresh window preserves the selected ledger and drafts in all existing tabs.
  event.waitUntil(self.clients.openWindow(candidate.href));
});

const shellCache = `npf-shell-${applicationVersion}`;
const shellFiles = ["/offline-shell", `/app.js?v=${applicationVersion}`, `/offline.js?v=${applicationVersion}`, `/styles.css?v=${applicationVersion}`, "/theme.js", "/pwa.webmanifest", "/assets/icon-192.png"];
async function clearShellCaches() {
  for (const name of await caches.keys()) if (name.startsWith("npf-shell-")) await caches.delete(name);
}
self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    if (offlineEnabled) await (await caches.open(shellCache)).addAll(shellFiles);
    else await clearShellCaches();
    await self.skipWaiting();
  })());
});
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) if (name.startsWith("npf-shell-") && (!offlineEnabled || name !== shellCache)) await caches.delete(name);
    await self.clients.claim();
  })());
});
self.addEventListener("fetch", (event) => {
  if (!offlineEnabled) return;
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  const navigation = event.request.mode === "navigate" && ["/", "/new", "/transactions", "/settings", "/pending", "/budget", "/simulator"].includes(url.pathname);
  const shellAsset = shellFiles.includes(url.pathname + url.search);
  if (!navigation && !shellAsset) return;
  event.respondWith((async () => {
    try {
      const response = await fetch(event.request);
      if (shellAsset && response.ok) {
        // Keep explicitly allowed offline assets current even between version bumps.
        try { await (await caches.open(shellCache)).put(event.request, response.clone()); } catch {}
      }
      return response;
    }
    catch {
      const cache = await caches.open(shellCache);
      return await cache.match(navigation ? "/offline-shell" : url.pathname + url.search) || Response.error();
    }
  })());
});
