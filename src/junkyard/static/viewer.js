"use strict";
const button = document.getElementById("copy-link");
const link = document.getElementById("share-link");
const status = document.getElementById("copy-status");
button.hidden = false;
button.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(link.value);
    status.textContent = "Share link copied.";
  } catch {
    link.focus();
    link.select();
    status.textContent = "Copy the selected link using your browser’s copy command.";
  }
});
