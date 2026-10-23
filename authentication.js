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
