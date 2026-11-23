"use strict";
const csrf = document.querySelector('meta[name="csrf-token"]').content;

async function request(path, method, body, json = false) {
  const headers = {"X-CSRF-Token": csrf, "Accept": "application/json"};
  if (json) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method, headers, body: json ? JSON.stringify(body) : body,
    credentials: "same-origin", redirect: "error"
  });
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try { message = (await response.json()).detail || message; } catch { /* Keep generic status. */ }
    if (response.status === 401) message += " Sign in again at /admin/login if your session expired.";
    if (response.status === 429) message += ` Retry after ${response.headers.get("Retry-After") || "a few"} seconds.`;
    throw new Error(message);
  }
  return response;
}

async function perform(button, status, operation) {
  button.disabled = true;
  status.textContent = "Working…";
  try { await operation(); }
  catch (error) { status.textContent = error.message || "Request failed. Reload to check the result before retrying."; }
  finally { button.disabled = false; }
}

for (const form of document.querySelectorAll(".admin-form")) {
  form.addEventListener("submit", event => {
    event.preventDefault();
    const data = new FormData(form);
    const status = form.querySelector(".form-status");
    perform(form.querySelector('button[type="submit"]'), status, async () => {
      if (form.id === "login-form") {
        await request("/admin/login", "POST", {password: data.get("password")}, true);
        form.reset();
        location.assign("/admin/");
      } else if (form.id === "password-form") {
        await request("/admin/cli-password", "POST", Object.fromEntries(data), true);
        form.reset();
        status.textContent = "CLI password changed.";
      } else {
        const fileInput = form.querySelector('input[type="file"]');
        const file = fileInput.files[0];
        const creating = form.id === "upload-form";
        const params = new URLSearchParams();
        const name = data.get("name");
        if (name) params.set("name", name);
        else if (creating && file) params.set("name", file.name);
        if (data.get("e")) params.set("e", data.get("e"));
        if (creating && data.get("secure")) params.set("s", "1");
        const path = creating ? "/admin/files" : `/admin/files/${form.dataset.id}`;
        await request(`${path}?${params}`, file ? "PUT" : "PATCH", file);
        location.assign("/admin/");
      }
    });
  });
}

for (const panel of document.querySelectorAll(".file-token")) {
  const endpoint = `/admin/files/${panel.dataset.id}/token`;
  const generate = panel.querySelector(".generate-token");
  const revoke = panel.querySelector(".revoke-token");
  const secret = panel.querySelector(".token-secret");
  const field = secret.querySelector("input");
  const status = panel.querySelector(".form-status");
  function clearSecret() {
    field.value = "";
    secret.hidden = true;
  }
  window.addEventListener("pagehide", () => {
    clearSecret();
    if (!revoke.hidden) status.textContent = "Token active.";
  });
  generate.addEventListener("click", () => perform(generate, status, async () => {
    const response = await request(endpoint, "POST");
    field.value = (await response.json()).token;
    generate.hidden = true;
    revoke.hidden = false;
    secret.hidden = false;
    status.textContent = "Token active.";
    field.focus();
    field.select();
  }));
  revoke.addEventListener("click", () => perform(revoke, status, async () => {
    await request(endpoint, "DELETE");
    clearSecret();
    revoke.hidden = true;
    generate.hidden = false;
    status.textContent = "Token revoked.";
    generate.focus();
  }));
  panel.querySelector(".copy-token").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(field.value);
      status.textContent = "Token copied.";
    } catch {
      field.focus();
      field.select();
      status.textContent = "Copy the selected token using your browser’s copy command.";
    }
  });
}

const adminStatus = document.getElementById("admin-status");
document.getElementById("logout")?.addEventListener("click", event => {
  perform(event.currentTarget, adminStatus, async () => {
    await request("/admin/logout", "POST");
    location.assign("/admin/login");
  });
});
for (const button of document.querySelectorAll(".delete-file")) {
  button.addEventListener("click", () => {
    if (!confirm("Delete this file? Its shared link will stop working.")) return;
    perform(button, adminStatus, async () => {
      await request(`/admin/files/${button.dataset.id}`, "DELETE");
      location.assign("/admin/");
    });
  });
}
