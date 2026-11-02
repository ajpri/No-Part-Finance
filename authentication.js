"use strict";
const registrationDialog = document.querySelector("#registrationDialog");
document.querySelector("[data-register]")?.addEventListener("click", (event) => {
  event.preventDefault();
  registrationDialog.showModal();
});
for (const dialog of document.querySelectorAll("dialog")) {
  if (dialog.hasAttribute("data-initial-dialog")) dialog.showModal();
  dialog.addEventListener("close", () => {
    if (dialog.hasAttribute("data-initial-dialog")) {
      if (dialog.id === "registrationDialog") location.assign("/sign-in");
      else location.assign(location.pathname);
    }
  });
  let backdropStarted = false;
  dialog.addEventListener("pointerdown", (event) => { backdropStarted = event.target === dialog; });
  dialog.addEventListener("pointerup", (event) => {
    if (backdropStarted && event.target === dialog) dialog.close();
    backdropStarted = false;
  });
}
document.querySelector("[data-cancel-registration]")?.addEventListener("click", (event) => {
  if (!registrationDialog.hasAttribute("data-initial-dialog")) {
    event.preventDefault();
    registrationDialog.close();
    registrationDialog.querySelector("form").reset();
  }
});

// Refresh administrator policy even when the returning installation is signed out.
OfflineFinance.ready.then(() => {
  if (window.isSecureContext && "serviceWorker" in navigator) return navigator.serviceWorker.register("/service-worker.js");
}).catch(() => {});

function preparePasswordReset() {
  const form = document.querySelector("[data-reset-password-form]");
  if (!form) return;
  const tokenInput = form.querySelector("[data-reset-token]");
  const tokenMessage = form.querySelector("[data-reset-token-message]");
  const submit = form.querySelector("[data-reset-submit]");
  const fragment = location.hash.slice(1);
  // Keep the emailed secret out of later URL sharing and navigation history.
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  if (/^[A-Za-z0-9_-]{43}$/.test(fragment)) tokenInput.value = fragment;
  const validToken = () => /^[A-Za-z0-9_-]{43}$/.test(tokenInput.value);
  const showMissingToken = () => {
    tokenMessage.textContent = "Open the reset link from your email, or request a new reset link.";
    tokenMessage.hidden = false;
    submit.disabled = true;
  };
  if (!validToken()) showMissingToken();
  form.addEventListener("submit", (event) => {
    if (!validToken()) { event.preventDefault(); showMissingToken(); }
  });
}
preparePasswordReset();
