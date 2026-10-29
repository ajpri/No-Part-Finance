/* Cache only explicit read-only snapshots. Never intercept or queue writes. */
const OfflineFinance = (() => {
  let policy = {pwaOfflineEnabled: false, pushNotificationsEnabled: false};
  try {
    const saved = JSON.parse(localStorage.getItem("npf-pwa-policy"));
    if (saved && typeof saved.pwaOfflineEnabled === "boolean" && typeof saved.pushNotificationsEnabled === "boolean") policy = saved;
  } catch {}
  let databasePromise;
  async function clearAll() {
    try { await records("readwrite", (store) => store.clear()); } catch {}
    try { if ("caches" in window) for (const name of await caches.keys()) if (name.startsWith("npf-shell-")) await caches.delete(name); } catch {}
  }
  async function refreshPolicy() {
    let fresh;
    try {
      const response = await fetch("/pwa-policy", {cache: "no-store", credentials: "same-origin"});
      if (!response.ok) return policy;
      fresh = await response.json();
      if (typeof fresh.pwaOfflineEnabled !== "boolean" || typeof fresh.pushNotificationsEnabled !== "boolean") return policy;
    } catch { return policy; } // Disconnected installations retain their last confirmed policy.
    const changed = policy.pwaOfflineEnabled !== fresh.pwaOfflineEnabled || policy.pushNotificationsEnabled !== fresh.pushNotificationsEnabled;
    policy = {pwaOfflineEnabled: fresh.pwaOfflineEnabled, pushNotificationsEnabled: fresh.pushNotificationsEnabled};
    try { localStorage.setItem("npf-pwa-policy", JSON.stringify(policy)); } catch {}
    if (!policy.pwaOfflineEnabled) await clearAll();
    if (changed && "serviceWorker" in navigator) {
      try { const registration = await navigator.serviceWorker.getRegistration(); if (registration) await registration.update(); } catch {}
    }
    return policy;
  }
  function database() {
    if (!databasePromise) databasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open("npf-offline", 1);
      request.onupgradeneeded = () => request.result.createObjectStore("snapshots", {keyPath: "key"});
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    return databasePromise;
  }
  async function records(mode, operation) {
    const connection = await database();
    return new Promise((resolve, reject) => {
      const transaction = connection.transaction("snapshots", mode);
      const request = operation(transaction.objectStore("snapshots"));
      transaction.oncomplete = () => resolve(request?.result);
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
  }
  async function save(session, dashboard, controls) {
    await ready;
    if (!policy.pwaOfflineEnabled || !window.isSecureContext || !("serviceWorker" in navigator) || !session?.userId || !session.ledgerId) return;
    const snapshot = {key: `${session.userId}:${session.ledgerId}`, userId: session.userId, ledgerId: session.ledgerId,
      session: {userId: session.userId, ledgerId: session.ledgerId, role: session.role, ledgers: session.ledgers},
      dashboard, controls, savedAt: new Date().toISOString()};
    await records("readwrite", (store) => store.put(snapshot));
    await records("readwrite", (store) => store.put({key: "active", userId: session.userId, ledgerId: session.ledgerId}));
  }
  async function available(userId = null) {
    await ready;
    if (!policy.pwaOfflineEnabled) return {active: null, snapshots: []};
    const active = await records("readonly", (store) => store.get("active"));
    if (userId === null) userId = active?.userId;
    const snapshots = await records("readonly", (store) => store.getAll());
    return {active, snapshots: snapshots.filter((item) => item.userId === userId && item.dashboard && item.session)};
  }
  async function clear(userId = null, ledgerId = null) {
    const {active, snapshots} = await available(userId);
    for (const snapshot of snapshots) if (!ledgerId || snapshot.ledgerId === ledgerId) await records("readwrite", (store) => store.delete(snapshot.key));
    if (active && (!userId || active.userId === userId) && (!ledgerId || active.ledgerId === ledgerId)) await records("readwrite", (store) => store.delete("active"));
  }
  const ready = refreshPolicy();
  return {save, available, clear, ready, refreshPolicy, get policy() { return policy; }};
})();
