// Optional Chrome integration driver using Node's built-in WebSocket (no npm packages).
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {readFile, writeFile, mkdir} from "node:fs/promises";
import {join} from "node:path";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const options = JSON.parse(input);
const profile = join(options.directory, "admin-chrome-profile");
await mkdir(profile);
const chrome = spawn(options.chrome, [
  "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
  "--disable-background-networking", "--disable-extensions", "--disable-sync",
  "--no-first-run", "--no-default-browser-check", "--disable-breakpad", "--no-proxy-server",
  "--ignore-certificate-errors", // Only the synthetic loopback HTTPS test certificate.
  "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
  "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"
], {stdio: "ignore", env: {...process.env,
  XDG_CONFIG_HOME: join(options.directory, "admin-chrome-config"),
  XDG_CACHE_HOME: join(options.directory, "admin-chrome-cache"),
  XDG_DATA_HOME: join(options.directory, "admin-chrome-data")
}});
let socket;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(action, label) {
  const deadline = Date.now() + 15000;
  do {
    try { if (await action()) return; } catch { /* Navigation can replace the execution context. */ }
    await sleep(100);
  } while (Date.now() < deadline);
  throw new Error(`Browser check timed out: ${label}`);
}
try {
  let port;
  await until(async () => {
    port = (await readFile(join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0];
    return port;
  }, "Chrome startup");
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  socket = new WebSocket(targets.find(target => target.type === "page").webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let nextId = 0;
  const pending = new Map();
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (!pending.has(message.id)) return;
    const {resolve, reject, timer} = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(timer);
    if (message.error) reject(new Error(message.error.message));
    else resolve(message.result);
  };
  const command = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++nextId;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 10000);
    pending.set(id, {resolve, reject, timer});
    socket.send(JSON.stringify({id, method, params}));
  });
  const evaluate = async expression => {
    const result = await command("Runtime.evaluate", {expression, awaitPromise: true, returnByValue: true, userGesture: true});
    if (result.exceptionDetails) throw new Error("Browser expression failed");
    return result.result.value;
  };
  const value = async (selector, text) => evaluate(`document.querySelector(${JSON.stringify(selector)}).value = ${JSON.stringify(text)}`);
  const click = selector => evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
  const selectFile = async (selector, path) => {
    const {root} = await command("DOM.getDocument");
    const {nodeId} = await command("DOM.querySelector", {nodeId: root.nodeId, selector});
    await command("DOM.setFileInputFiles", {nodeId, files: [path]});
  };
  await command("Page.enable");
  await command("Emulation.setDeviceMetricsOverride", {width: 1200, height: 1000, deviceScaleFactor: 1, mobile: false});
  await command("Page.navigate", {url: options.origin + "/admin/login"});
  await until(() => evaluate('document.querySelector("#login-form") && document.readyState === "complete"'), "login page");
  await value("#password", options.browserPassword);
  await click('#login-form button[type="submit"]');
  await until(() => evaluate('location.pathname === "/admin/" && !!document.querySelector("#upload-form")'), "login");
  assert.equal(await evaluate('document.cookie.includes("junkyard-session")'), false);
  await selectFile("#upload-file", options.source);
  await value("#upload-expiration", "0");
  await click('#upload-form button[type="submit"]');
  await until(() => evaluate('!!document.querySelector(".file-entry")'), "upload");
  const id = await evaluate('document.querySelector(".edit-form").dataset.id');
  assert.equal(await evaluate(`fetch("/${id}/raw").then(r => r.text())`), "original browser bytes");
  await evaluate('document.querySelector("details").open = true');
  await value(`#name-${id}`, "renamed.txt");
  await click('.edit-form button[type="submit"]');
  await until(() => evaluate('document.querySelector(".file-entry h3")?.textContent === "renamed.txt"'), "metadata edit");
  await evaluate('document.querySelector("details").open = true');
  await selectFile(`#replace-${id}`, options.replacement);
  await click('.edit-form button[type="submit"]');
  await until(() => evaluate(`fetch("/${id}/raw").then(r => r.text()).then(s => s === "replacement browser bytes")`), "replacement");
  await until(() => evaluate('document.readyState === "complete" && !!document.querySelector("#password-form")'), "replacement navigation");
  await value("#browser-password", options.browserPassword);
  await value("#new-password", options.newPassword);
  await value("#confirmation", options.newPassword);
  await click('#password-form button[type="submit"]');
  await until(() => evaluate('document.querySelector("#password-form .form-status").textContent === "CLI password changed."'), "password rotation");
  const screenshot = await command("Page.captureScreenshot", {captureBeyondViewport: true});
  await writeFile(join(options.directory, "admin.png"), Buffer.from(screenshot.data, "base64"));
  // Accept only the explicit confirmation triggered by this test's delete click.
  await evaluate('window.confirm = () => true');
  await click(".delete-file");
  await until(() => evaluate('document.querySelector("#upload-form") && !document.querySelector(".file-entry")'), "delete");
  assert.equal(await evaluate(`fetch("/${id}").then(r => r.status)`), 404);
  await click("#logout");
  await until(() => evaluate('location.pathname === "/admin/login" && !!document.querySelector("#login-form")'), "logout");
  await command("Page.navigate", {url: options.origin + "/admin/"});
  await until(() => evaluate('location.pathname === "/admin/login" && document.readyState === "complete"'), "logout revocation");
  console.log("Browser login, upload, edit, replacement, rotation, deletion, and logout passed.");
} finally {
  socket?.close();
  chrome.kill("SIGTERM");
  await Promise.race([new Promise(resolve => chrome.once("exit", resolve)), sleep(2000)]);
  if (chrome.exitCode === null) chrome.kill("SIGKILL");
}
