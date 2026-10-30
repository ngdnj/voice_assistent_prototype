import assert from "node:assert/strict";
import { test } from "node:test";
import { execFileSync } from "node:child_process";
import { createLongPress } from "../src/longPress.ts";
import { formatMoment } from "../src/formatMoment.ts";

const record = {
  id: "task-1", event_type: "task", name: "Проверка", description: "",
  status: "todo", date: null, datetime: null, end_time: null,
  created_at: "2026-10-30T10:00:00Z",
};

test("long press reveals once, while releasing early cancels", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let revealed = 0;
  const press = createLongPress(() => revealed++);
  press.start(1, 20, 20);
  t.mock.timers.tick(549);
  assert.equal(revealed, 0);
  press.cancel(1);
  t.mock.timers.tick(1000);
  assert.equal(revealed, 0);
  press.start(1, 20, 20);
  t.mock.timers.tick(550);
  assert.equal(revealed, 1);
  t.mock.timers.tick(1000);
  assert.equal(revealed, 1);
});

test("movement, scrolling cleanup and a replacement gesture cancel a hold", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let revealed = 0;
  const press = createLongPress(() => revealed++);
  press.start(1, 20, 20);
  press.move(1, 20, 31);
  t.mock.timers.tick(600);
  assert.equal(revealed, 0);
  press.start(1, 20, 20);
  press.cancel(); // scroll / blur / component unmount
  t.mock.timers.tick(600);
  assert.equal(revealed, 0);
  press.start(1, 20, 20);
  t.mock.timers.tick(500);
  press.start(2, 20, 20);
  t.mock.timers.tick(50);
  assert.equal(revealed, 0);
  press.cancel(1); // releasing another pointer must not cancel the active one
  t.mock.timers.tick(500);
  assert.equal(revealed, 1);
});

test("small pointer jitter does not cancel an intentional hold", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let revealed = 0;
  const press = createLongPress(() => revealed++);
  press.start(1, 20, 20);
  press.move(1, 23, 24);
  t.mock.timers.tick(550);
  assert.equal(revealed, 1);
});

test("tasks and meetings display both range boundaries", () => {
  const start = "2026-10-30T10:00:00Z";
  const end = "2026-10-30T12:00:00Z";
  const time = (value) => new Date(value).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  for (const event_type of ["task", "meeting"]) {
    const formatted = formatMoment({ ...record, event_type, datetime: start, end_time: end });
    assert.ok(formatted.includes(`${time(start)}–${time(end)}`), formatted);
  }
});

test("a range crossing local midnight displays both dates and the year", () => {
  const start = new Date(2026, 11, 31, 23, 0);
  const end = new Date(2027, 0, 1, 1, 0);
  const formatted = formatMoment({ ...record, datetime: start.toISOString(), end_time: end.toISOString() });
  assert.match(formatted, /31 декабря/);
  assert.match(formatted, /1 января/);
  assert.match(formatted, /2026/);
  assert.match(formatted, /2027/);
  assert.match(formatted, /23:00/);
  assert.match(formatted, /01:00/);
});

test("a date-only task keeps its local calendar date even west of UTC", () => {
  const formatted = execFileSync(process.execPath, ["--input-type=module", "-e",
    `import { formatMoment } from ${JSON.stringify(new URL("../src/formatMoment.ts", import.meta.url).href)};
    console.log(formatMoment(${JSON.stringify({ ...record, date: "2026-10-30" })}));`,
  ], { env: { ...process.env, TZ: "America/Los_Angeles" }, encoding: "utf8" });
  assert.match(formatted, /30 октября/);
  assert.match(formatted, /без времени/);
});

test("missing deadline and a single instant remain distinct", () => {
  assert.equal(formatMoment(record), "Без срока");
  assert.ok(!formatMoment({ ...record, datetime: "2026-10-30T10:00:00Z" }).includes("–"));
});
