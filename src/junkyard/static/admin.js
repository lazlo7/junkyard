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
