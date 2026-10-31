// Optional local Chromium smoke: npm run build && node tests/browser-smoke.mjs
// Uses only Node built-ins and an installed chromedriver, with a fake local HTTP API.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
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

const edits = [], deletions = [], operations = new Map();
let firstEditFailure = true, firstDeleteFailure = true, wrongDeleteId = true;
let holdNextRead = false, heldRead = null;
function apply(record, changes) {
  if (changes.name) record.name = changes.name;
  if (changes.clear_schedule) Object.assign(record, { date: null, datetime: null, end_time: null });
  if (changes.date) Object.assign(record, { date: changes.date, datetime: null, end_time: null });
  if (changes.datetime) Object.assign(record, { datetime: changes.datetime, date: null });
  if (changes.end_time) record.end_time = changes.end_time;
  if (changes.clear_end_time) record.end_time = null;
}
const app = createServer(async (request, response) => {
  if (request.url.startsWith('/api/')) {
    response.setHeader('Content-Type', 'application/json');
    assert.equal(request.headers['x-max-init-data'], 'synthetic-test-init-data');
    const url = new URL(request.url, 'http://test');
    const id = decodeURIComponent(url.pathname.split('/').at(-1));
    if (request.method === 'PATCH') {
      const chunks = []; for await (const chunk of request) chunks.push(chunk);
      const changes = JSON.parse(Buffer.concat(chunks).toString());
      if (url.pathname.endsWith('/status')) {
        const record = records.find(r => r.id === url.pathname.split('/')[3]);
        record.status = changes.status; response.end(JSON.stringify({event: record})); return;
      }
      const operation = request.headers['x-action-id']; assert.match(operation, /^[0-9a-f-]{36}$/);
      assert.equal(url.searchParams.get('timezone'), 'Europe/Moscow'); edits.push({id, operation, changes});
      const record = records.find(r => r.id === id);
      if (operations.has(operation)) assert.deepEqual(operations.get(operation), changes);
      else { operations.set(operation, changes); apply(record, changes); }
      if (firstEditFailure) {
        firstEditFailure = false; response.statusCode = 503;
        response.end(JSON.stringify({error:{message:'Связь прервалась. Повторите сохранение.'}}));
      } else response.end(JSON.stringify({updated:record}));
      return;
    }
    if (request.method === 'DELETE') {
      const operation = request.headers['x-action-id']; assert.match(operation, /^[0-9a-f-]{36}$/);
      deletions.push({id, operation});
      if (firstDeleteFailure) {
        firstDeleteFailure = false; response.statusCode = 503; response.end(JSON.stringify({error:{message:'Временная ошибка'}}));
      } else {
        const record = records.find(r => r.id === id);
        response.end(JSON.stringify({deleted: wrongDeleteId ? {...record,id:'wrong-id'} : record})); wrongDeleteId = false;
      }
      return;
    }
    const query = url.searchParams.get('query');
    const result = query ? records.filter(r => r.name.toLowerCase().includes(query.toLowerCase())) : records.filter(r => {
      const moment = r.datetime ?? (r.date ? `${r.date}T12:00:00Z` : null);
      return moment && new Date(moment) >= new Date(url.searchParams.get('start_time')) && new Date(moment) < new Date(url.searchParams.get('end_time'));
    });
    const payload = JSON.stringify({events:result});
    // Deleted records deliberately remain in stale responses; edits return the updated data.
    if (holdNextRead) { holdNextRead = false; heldRead = () => response.end(payload); }
    else response.end(payload);
    return;
  }
  try {
    const path = request.url === '/' ? 'index.html' : decodeURIComponent(request.url.split('?')[0]).slice(1);
    if (path.includes('..')) throw new Error('bad path');
    let data = await readFile(join(root, path));
    if (path === 'index.html') data = Buffer.from(data.toString().replace('<script src="https://st.max.ru/js/max-web-app.js"></script>', ''));
    response.setHeader('Content-Type', path.endsWith('.js') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : 'text/html');
    response.end(data);
  } catch { response.statusCode = 404; response.end(); }
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
async function switchTab(number) {
  await click(`.tab:nth-child(${number})`);
  await waitFor(() => run("return !document.querySelector('.status-message')"), "tab loaded");
}

async function setValue(selector, value) {
  await run(`const e=document.querySelector(arguments[0]);const p=e.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(p,'value').set.call(e,arguments[1]); e.dispatchEvent(new Event(e.tagName==='SELECT'?'change':'input',{bubbles:true}));`,[selector,value]);
}
async function key(selector,text) {
  const item=await element(selector); await command(`/session/${session}/element/${Object.values(item)[0]}/value`,{text});
}
async function menu(row=1) { await click(`.event-row:nth-child(${row}) .event-menu-trigger`); }
async function edit(row=1) { await menu(row); await click('[role=menuitem]:first-child'); }
async function save() { await click('.editor-save'); }

try {
  await waitFor(async () => { try { return (await fetch(driverBase+'/status')).ok; } catch {return false;} }, 'driver start');
  session=(await command('/session',{capabilities:{alwaysMatch:{browserName:'chrome','goog:chromeOptions':{
    binary:process.env.CHROME_BIN??'/usr/bin/chromium',args:['--headless=new','--disable-background-networking','--no-proxy-server','--window-size=450,850']
  }}}})).sessionId;
  await command(`/session/${session}/goog/cdp/execute`,{cmd:'Page.addScriptToEvaluateOnNewDocument',params:{source:
    "window.WebApp={initData:'synthetic-test-init-data'};const OriginalDate=Date;window.Date=class extends OriginalDate{constructor(...a){super(...(a.length?a:['2026-10-30T09:00:00Z']));}static now(){return new OriginalDate('2026-10-30T09:00:00Z').getTime();}};"}});
  await command(`/session/${session}/goog/cdp/execute`,{cmd:'Emulation.setTimezoneOverride',params:{timezoneId:'Europe/Moscow'}});
  await command(`/session/${session}/url`,{url:base});
  await waitFor(()=>run("return document.querySelectorAll('.event-row').length===2"),'initial tasks');
  assert.match(await run("return document.querySelector('.event-moment').textContent"),/15:00–17:00/);
  await menu(); assert.deepEqual(await run("return [...document.querySelectorAll('[role=menuitem]')].map(e=>e.textContent)"),['Редактировать','Удалить']);
  assert.equal(deletions.length,0);
  await key('[role=menuitem]:first-child','\uE00C');
  assert.equal(await run("return !!document.querySelector('[role=menu]')"),false);
  await menu();await click('h1');assert.equal(await run("return !!document.querySelector('[role=menu]')"),false);
  holdNextRead=true;await setValue('.status-select','in_progress');await waitFor(()=>!!heldRead,'old GET held');
  await edit();await setValue('[name=name]','Новый отчёт');await setValue('[name=time]','16:00');await setValue('[name=endTime]','18:00');
  assert.equal(await run("return document.querySelector('dialog').scrollWidth<=document.querySelector('dialog').clientWidth"),true);
  await writeFile('/tmp/miniapp-editor-preview.png',Buffer.from(await command(`/session/${session}/screenshot`,undefined,'GET'),'base64'));
  await command(`/session/${session}/goog/cdp/execute`,{cmd:'Emulation.setDeviceMetricsOverride',params:{width:360,height:800,deviceScaleFactor:1,mobile:true}});
  await run("document.documentElement.dataset.theme='dark'");
  assert.equal(await run("return document.querySelector('dialog').scrollWidth<=document.querySelector('dialog').clientWidth"),true);
  assert.equal(await run("return getComputedStyle(document.querySelector('#editor-heading')).color"),
    await run("return getComputedStyle(document.querySelector('dialog')).color"));
  await writeFile('/tmp/miniapp-editor-mobile-dark.png',Buffer.from(await command(`/session/${session}/screenshot`,undefined,'GET'),'base64'));
  await command(`/session/${session}/goog/cdp/execute`,{cmd:'Emulation.clearDeviceMetricsOverride',params:{}});
  await run("document.documentElement.dataset.theme='light'");
  await save();await waitFor(()=>run("return !!document.querySelector('dialog [role=alert]')"),'save error');
  assert.equal(await run("return document.querySelector('[name=name]').value"),'Новый отчёт');
  await save();await waitFor(()=>run("return !document.querySelector('dialog')&&document.querySelector('.event-name')?.textContent==='Новый отчёт'"),'save succeeds');
  heldRead();heldRead=null;await pause(500);
  assert.equal(await run("return document.querySelector('.event-name').textContent"),'Новый отчёт');
  assert.equal(edits[0].operation,edits[1].operation);
  assert.deepEqual(edits[0].changes,{name:'Новый отчёт',datetime:'2026-10-30T13:00:00.000Z',end_time:'2026-10-30T15:00:00.000Z'});
  await edit();await setValue('[name=schedule]','date');await setValue('[name=date]','2026-10-31');await save();
  await waitFor(()=>run("return !document.querySelector('dialog')&&document.querySelector('.event-moment')?.textContent.includes('без времени')"),'date-only saved');
  assert.notEqual(edits[2].operation,edits[1].operation);
  await edit();await setValue('[name=schedule]','none');await save();
  await waitFor(()=>run("return document.querySelectorAll('.event-row').length===1"),'unscheduled leaves period');
  await setValue('.search-input','Новый отчёт');await waitFor(()=>run("return document.querySelector('.event-moment')?.textContent==='Без срока'"),'unscheduled searchable');
  await edit();await setValue('[name=name]','Обновлённая задача');await save();
  await waitFor(()=>run("return !!document.querySelector('.empty-state')"),'rename leaves old search');
  await setValue('.search-input','');await waitFor(()=>run("return document.querySelector('.event-name')?.textContent==='Купить хлеб'"),'remaining task');
  await edit();await setValue('[name=name]','Не сохранять');await key('[name=name]','\uE00C');
  assert.equal(await run("return !!document.querySelector('dialog')"),false);
  assert.equal(await run("return document.querySelector('.event-name').textContent"),'Купить хлеб');
  await run("document.querySelector('.event-menu-trigger').focus()");await key('.event-menu-trigger','\uE007');
  await key('[role=menuitem]:first-child','\uE015');assert.equal(await run('return document.activeElement.textContent'),'Удалить');
  await key('.event-menu-delete','\uE00C');assert.equal(await run("return document.activeElement.classList.contains('event-menu-trigger')"),true);
  for(let i=0;i<3;i++) {
    await menu();await click('.event-menu-delete');
    await waitFor(()=>run(i<2?"return !!document.querySelector('[role=alert]')":"return !!document.querySelector('.empty-state')"),'delete attempt');
    if(i<2) assert.equal(await run("return document.querySelectorAll('.event-row').length"),1);
  }
  assert.equal(deletions[0].operation,deletions[1].operation);assert.equal(deletions[1].operation,deletions[2].operation);
  await switchTab(2);await edit();await setValue('[name=name]','Новая встреча');await save();
  await waitFor(()=>run("return !document.querySelector('dialog')"),'meeting renamed');
  await menu();await click('.event-menu-delete');await waitFor(()=>run("return !!document.querySelector('.empty-state')"),'meeting deleted');
  await switchTab(3);const trigger=await element('.event-menu-trigger');
  await command(`/session/${session}/actions`,{actions:[{type:'pointer',id:'touch',parameters:{pointerType:'touch'},actions:[
    {type:'pointerMove',origin:trigger,x:0,y:0},{type:'pointerDown',button:0},{type:'pointerUp',button:0}]}]});
  await waitFor(()=>run("return !!document.querySelector('[role=menu]')"),'touch opens menu');
  await click('[role=menuitem]:first-child');assert.equal(await run("return !!document.querySelector('[name=range]')"),false);
  await setValue('[name=time]','16:30');await save();await waitFor(()=>run("return !document.querySelector('dialog')"),'reminder time');
  await menu();await click('.event-menu-delete');await waitFor(()=>run("return !!document.querySelector('.empty-state')"),'reminder deleted');
  console.log('Browser smoke passed: menu/edit/delete, ranges/date-only/no deadline, retries, stale GET, filtering, keyboard/touch, all types.');
} catch(error) {
  console.error(driverLog.slice(-1000));
  if(session) {
    console.error(await run("return {text:document.body.innerText,html:document.documentElement.outerHTML.slice(-2200)}"));
    await writeFile('/tmp/miniapp-edit-failure.png',Buffer.from(await command(`/session/${session}/screenshot`,undefined,'GET'),'base64'));
  }
  throw error;
} finally {
  if(heldRead)heldRead();
  if(session)await command(`/session/${session}`,undefined,'DELETE').catch(()=>{});
  driver.kill();await new Promise(resolve=>app.close(resolve));
}
