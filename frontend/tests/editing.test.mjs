import assert from "node:assert/strict";
import { test } from "node:test";
import { execFileSync } from "node:child_process";
import { buildEventPatch, eventDraft } from "../src/eventEditing.ts";

const task = { id: "task-1", event_type: "task", name: "Отчёт", description: "", status: "todo",
  date: null, datetime: null, end_time: null, created_at: "2026-10-31T10:00:00Z" };
function inZone(zone, code) {
  return execFileSync(process.execPath, ["--input-type=module", "-e",
    `import { localInstant, buildEventPatch, eventDraft } from ${JSON.stringify(new URL("../src/eventEditing.ts", import.meta.url).href)}; ${code}`,
  ], { env: { ...process.env, TZ: zone }, encoding: "utf8" }).trim();
}

test("renaming preserves timestamps and seconds by sending only the changed name", () => {
  const record = { ...task, datetime: "2026-11-01T10:00:42Z", end_time: "2026-11-01T12:00:42Z" };
  assert.deepEqual(buildEventPatch(record, { ...eventDraft(record), name: "  Новый отчёт  " }), { name: "Новый отчёт" });
  assert.deepEqual(buildEventPatch(record, eventDraft(record)), {});
});
test("date-only and no-deadline modes are explicit and never invent midnight", () => {
  const timed = { ...task, datetime: "2026-11-01T10:00:00Z", end_time: "2026-11-01T12:00:00Z" };
  assert.deepEqual(buildEventPatch(timed, { ...eventDraft(timed), mode: "date", date: "2026-11-03" }), { date: "2026-11-03" });
  assert.deepEqual(buildEventPatch(timed, { ...eventDraft(timed), mode: "none" }), { clear_schedule: true });
  assert.deepEqual(buildEventPatch(task, eventDraft(task)), {});
});
test("removing an interval does not remove its start", () => {
  const record = { ...task, datetime: "2026-11-01T10:00:00Z", end_time: "2026-11-01T12:00:00Z" };
  assert.deepEqual(buildEventPatch(record, { ...eventDraft(record), range: false }), { clear_end_time: true });
});
test("local dates and times become exact UTC instants", () => {
  const output = inZone("Europe/Moscow", `const r=${JSON.stringify(task)};
    console.log(JSON.stringify(buildEventPatch(r,{...eventDraft(r),mode:'time',date:'2026-11-02',time:'10:30'})));`);
  assert.deepEqual(JSON.parse(output), { datetime: "2026-11-02T07:30:00.000Z" });
  const west = inZone("America/Los_Angeles", `console.log(localInstant('2026-11-02','23:30'));`);
  assert.equal(west, "2026-11-03T07:30:00.000Z");
});
test("a date without time does not shift in western zones", () => {
  const output = inZone("America/Los_Angeles", `const r={...${JSON.stringify(task)},date:'2026-11-01'};
    console.log(JSON.stringify(buildEventPatch(r,{...eventDraft(r),date:'2026-11-02'})));`);
  assert.deepEqual(JSON.parse(output), { date: "2026-11-02" });
});
test("invalid names, calendar dates, incomplete times and reversed intervals fail", () => {
  for (const change of [{ name: " " }, { mode: "date", date: "2026-02-30" }, { mode: "time", time: "" },
    { mode: "time", date: "2026-11-01", time: "12:00", range: true, endDate: "2026-11-01", endTime: "11:00" }]) {
    assert.throws(() => buildEventPatch(task, { ...eventDraft(task), ...change }));
  }
  assert.throws(() => buildEventPatch({ ...task, event_type: "meeting" }, { ...eventDraft(task), mode: "none" }));
});
test("nonexistent and ambiguous local times are rejected rather than silently selected", () => {
  const output = inZone("America/New_York", `
    const messages=[];
    for(const [d,t] of [['2026-03-08','02:30'],['2026-11-01','01:30']]) {
      try { localInstant(d,t); messages.push('unexpected success'); } catch(e) { messages.push(e.message); }
    }
    console.log(JSON.stringify(messages));`);
  const messages = JSON.parse(output);
  assert.match(messages[0], /времени нет/);
  assert.match(messages[1], /встречается дважды/);
});
test("an existing explicit instant in a DST fold can be renamed without changing its offset", () => {
  const output = inZone("America/New_York", `const r={...${JSON.stringify(task)},datetime:'2026-11-01T05:30:00Z'};
    console.log(JSON.stringify(buildEventPatch(r,{...eventDraft(r),name:'Новое'})));`);
  assert.deepEqual(JSON.parse(output), { name: "Новое" });
});
