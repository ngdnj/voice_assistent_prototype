// Optional local Chromium smoke: npm run build && node tests/browser-smoke.mjs
// Uses only Node built-ins and an installed chromedriver, with a fake local HTTP API.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { createServer as createPortReservation } from "node:net";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const root = fileURLToPath(new URL("../dist/", import.meta.url));
const records = [
  { id: "task-range", event_type: "task", name: "Подготовить отчёт", status: "todo",
    datetime: "2026-10-30T12:00:00Z", end_time: "2026-10-30T14:00:00Z", date: null },
  { id: "task-date", event_type: "task", name: "Купить хлеб", status: "todo",
    datetime: null, end_time: null, date: "2026-10-30" },
  { id: "meeting", event_type: "meeting", name: "Ночной созвон", status: null,
    datetime: "2026-10-30T20:00:00Z", end_time: "2026-10-30T22:00:00Z", date: null },
  { id: "reminder", event_type: "reminder", name: "Позвонить врачу", status: null,
    datetime: "2026-10-30T12:30:00Z", end_time: null, date: null },
].map((record) => ({ ...record, description: "", created_at: "2026-10-30T09:00:00Z" }));
const deletions = [];
let firstFailure = true;

const app = createServer(async (request, response) => {
  if (request.url.startsWith("/api/")) {
    response.setHeader("Content-Type", "application/json");
    assert.equal(request.headers["x-max-init-data"], "synthetic-test-init-data");
    if (request.method === "DELETE") {
      const id = decodeURIComponent(request.url.split("/").at(-1));
      const operation = request.headers["x-action-id"];
      assert.match(operation, /^[0-9a-f-]{36}$/);
      deletions.push({ id, operation });
      if (firstFailure) {
        firstFailure = false;
        response.statusCode = 503;
        response.end(JSON.stringify({ error: { message: "Synthetic temporary failure" } }));
      } else {
        const deleted = records.find((record) => record.id === id);
        response.end(JSON.stringify({ deleted: id === "task-date" ? { ...deleted, id: "wrong-id" } : deleted }));
      }
      return;
    }
    // Intentionally stale responses: client must not restore a confirmed deletion.
    response.end(JSON.stringify({ events: records }));
    return;
  }
  try {
    const path = request.url === "/" ? "index.html" : decodeURIComponent(request.url.split("?")[0]).slice(1);
    if (path.includes("..")) throw new Error("bad path");
    let data = await readFile(join(root, path));
    if (path === "index.html") {
      // The test supplies synthetic initData. Never call the real MAX Bridge from a UI smoke.
      data = Buffer.from(data.toString().replace('<script src="https://st.max.ru/js/max-web-app.js"></script>', ""));
    }
    response.setHeader("Content-Type", path.endsWith(".js") ? "text/javascript" : path.endsWith(".css") ? "text/css" : "text/html");
    response.end(data);
  } catch {
    response.statusCode = 404;
    response.end();
  }
});
await new Promise((resolve) => app.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${app.address().port}`;
const reservation = createPortReservation();
await new Promise((resolve) => reservation.listen(0, "127.0.0.1", resolve));
const driverPort = reservation.address().port;
await new Promise((resolve) => reservation.close(resolve));
const driver = spawn(process.env.CHROMEDRIVER ?? "chromedriver", [`--port=${driverPort}`], { stdio: "pipe" });
let driverLog = "";
driver.stdout.on("data", (data) => { driverLog += data; });
driver.stderr.on("data", (data) => { driverLog += data; });
const driverBase = `http://127.0.0.1:${driverPort}`;
let session;
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitFor(check, label) {
  const deadline = Date.now() + 6000;
  while (Date.now() < deadline) {
    if (await check()) return;
    await pause(50);
  }
  throw new Error(`Timeout: ${label}`);
}
async function command(path, body, method = "POST") {
  const response = await fetch(driverBase + path, {
    method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (result.value?.error) throw new Error(JSON.stringify(result.value));
  return result.value;
}
const run = (script, args = []) => command(`/session/${session}/execute/sync`, { script, args });
const element = (selector) => command(`/session/${session}/element`, { using: "css selector", value: selector });
async function click(selector) {
  const item = await element(selector);
  await command(`/session/${session}/element/${Object.values(item)[0]}/click`, {});
}
async function hold(selector, move = 0, pointerType = "mouse") {
  const item = await element(selector);
  await command(`/session/${session}/actions`, { actions: [{ type: "pointer", id: pointerType, parameters: { pointerType }, actions: [
    { type: "pointerMove", origin: item, x: 0, y: 0 }, { type: "pointerDown", button: 0 },
    ...(move ? [{ type: "pointerMove", origin: "pointer", x: 0, y: move }] : []),
    { type: "pause", duration: 650 }, { type: "pointerUp", button: 0 },
  ] }] });
}
async function switchTab(number) {
  await click(`.tab:nth-child(${number})`);
  await waitFor(() => run("return !document.querySelector('.status-message')"), "tab loaded");
}

try {
  await waitFor(async () => {
    try { return (await fetch(driverBase + "/status")).ok; } catch { return false; }
  }, "chromedriver start");
  const created = await command("/session", { capabilities: { alwaysMatch: {
    browserName: "chrome", "goog:chromeOptions": {
      binary: process.env.CHROME_BIN ?? "/usr/bin/chromium",
      args: ["--headless=new", "--disable-background-networking", "--no-proxy-server", "--window-size=450,850"],
    },
  } } });
  session = created.sessionId;
  await command(`/session/${session}/goog/cdp/execute`, { cmd: "Page.addScriptToEvaluateOnNewDocument", params: { source: "window.WebApp = {initData: 'synthetic-test-init-data'};" } });
  await command(`/session/${session}/goog/cdp/execute`, { cmd: "Emulation.setTimezoneOverride", params: { timezoneId: "Europe/Moscow" } });
  await command(`/session/${session}/url`, { url: base });
  await waitFor(() => run("return document.querySelectorAll('.event-row').length === 2"), "records loaded");
  assert.match(await run("return document.querySelector('.event-moment').textContent"), /15:00–17:00/);
  assert.match(await run("return document.querySelectorAll('.event-moment')[1].textContent"), /30 октября.*без времени/);

  await hold(".event-row:first-child .event-card", 25);
  assert.equal(await run("return !!document.querySelector('.event-delete')"), false);
  await hold(".event-row:first-child .event-card");
  assert.equal(deletions.length, 0); // Holding only reveals; it does not delete.
  assert.equal(await run("const b=document.querySelector('.event-delete').getBoundingClientRect(); const c=document.querySelector('.event-card').getBoundingClientRect(); return b.right < c.left"), true);
  await click(".event-delete");
  await waitFor(() => run("return !!document.querySelector('[role=alert]')"), "delete error shown");
  assert.equal(await run("return document.querySelectorAll('.event-row').length"), 2);
  await click(".event-delete");
  await waitFor(() => run("return document.querySelectorAll('.event-row').length === 1"), "delete confirmed");
  assert.equal(deletions[0].operation, deletions[1].operation);

  await switchTab(2);
  assert.match(await run("return document.querySelector('.event-moment').textContent"), /30 октября.*23:00.*31 октября.*01:00/);
  await switchTab(1);
  assert.equal(await run("return document.querySelectorAll('.event-row').length"), 1);
  assert.equal(await run("return document.querySelector('.event-name').textContent"), "Купить хлеб");
  const card = await element(".event-card");
  await run("document.querySelector('.event-card').focus()");
  await command(`/session/${session}/element/${Object.values(card)[0]}/value`, { text: "\uE007" });
  await waitFor(() => run("return document.activeElement?.classList.contains('event-delete')"), "keyboard delete focus");
  const trash = await element(".event-delete");
  await command(`/session/${session}/element/${Object.values(trash)[0]}/value`, { text: "\uE00C" });
  await waitFor(() => run("return !document.querySelector('.event-delete')"), "Escape hides delete action");
  await command(`/session/${session}/element/${Object.values(card)[0]}/value`, { text: "\uE007" });
  await waitFor(() => run("return document.activeElement?.classList.contains('event-delete')"), "keyboard reopens delete");
  await click(".event-delete");
  await waitFor(() => run("return !!document.querySelector('[role=alert]')"), "wrong deleted ID rejected");
  assert.equal(await run("return document.querySelectorAll('.event-row').length"), 1);

  await switchTab(2);
  await hold(".event-card");
  await click(".event-delete");
  await waitFor(() => run("return !!document.querySelector('.empty-state')"), "meeting deleted");
  await switchTab(3);
  await hold(".event-card", 0, "touch");
  await click(".event-delete");
  await waitFor(() => run("return !!document.querySelector('.empty-state')"), "reminder deleted");
  console.log("Browser smoke passed: ranges, long press/cancel, left trash, retry UUID, errors, stale GET, keyboard, all types.");
} catch (error) {
  console.error(driverLog.slice(-2500));
  if (session) console.error(await run("return {text:document.body.innerText, webapp:!!window.WebApp, html:document.documentElement.outerHTML.slice(0,1500)}"));
  throw error;
} finally {
  if (session) await command(`/session/${session}`, undefined, "DELETE").catch(() => {});
  driver.kill();
  await new Promise((resolve) => app.close(resolve));
}
