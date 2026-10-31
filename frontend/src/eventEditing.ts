import type { EventRecord } from "./types";

export type ScheduleMode = "none" | "date" | "time";
export interface EventDraft {
  name: string;
  mode: ScheduleMode;
  date: string;
  time: string;
  range: boolean;
  endDate: string;
  endTime: string;
}
export interface EventPatch {
  name?: string;
  date?: string;
  datetime?: string;
  end_time?: string;
  clear_schedule?: boolean;
  clear_end_time?: boolean;
}

const pad = (value: number) => String(value).padStart(2, "0");
const localDate = (value: Date) => `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`;
const localTime = (value: Date) => `${pad(value.getHours())}:${pad(value.getMinutes())}`;

export function eventDraft(event: EventRecord): EventDraft {
  const start = event.datetime ? new Date(event.datetime) : null;
  const end = event.end_time ? new Date(event.end_time) : null;
  const date = event.date ?? localDate(start ?? new Date());
  return {
    name: event.name,
    mode: start || event.event_type !== "task" ? "time" : event.date ? "date" : "none",
    date, time: start ? localTime(start) : "", range: !!end,
    endDate: end ? localDate(end) : date, endTime: end ? localTime(end) : "",
  };
}

export function localInstant(day: string, time: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || !/^\d{2}:\d{2}$/.test(time)) {
    throw new Error("Укажите дату и время.");
  }
  const [year, month, date] = day.split("-").map(Number);
  const [hour, minute] = time.split(":").map(Number);
  if (year < 1 || month < 1 || month > 12 || date < 1 || date > 31 || hour > 23 || minute > 59) {
    throw new Error("Проверьте дату и время.");
  }
  const base = new Date(0);
  base.setUTCFullYear(year, month - 1, date);
  base.setUTCHours(hour, minute, 0, 0);
  // Inspect both offsets around a clock change instead of accepting Date's silent choice.
  const offsets = new Set<number>();
  for (let hours = -36; hours <= 36; hours += 6) {
    offsets.add(new Date(base.getTime() + hours * 3600000).getTimezoneOffset());
  }
  const candidates = [...offsets].map((offset) => new Date(base.getTime() + offset * 60000)).filter((d) =>
    d.getFullYear() === year && d.getMonth() === month - 1 && d.getDate() === date &&
    d.getHours() === hour && d.getMinutes() === minute && d.getSeconds() === 0);
  if (candidates.length === 0) throw new Error("Такой даты или местного времени нет. Проверьте перевод часов.");
  if (candidates.length > 1) throw new Error("Это время встречается дважды из-за перевода часов. Выберите другое время.");
  return candidates[0].toISOString();
}

export function buildEventPatch(event: EventRecord, draft: EventDraft): EventPatch {
  const name = draft.name.trim();
  if (!name || name.length > 200) throw new Error("Название должно содержать от 1 до 200 символов.");
  const original = eventDraft(event);
  const patch: EventPatch = {};
  if (name !== event.name) patch.name = name;
  if (event.event_type !== "task" && draft.mode !== "time") throw new Error("Укажите дату и время записи.");
  if (draft.mode === "none") {
    if (original.mode !== "none") patch.clear_schedule = true;
    return patch;
  }
  if (draft.mode === "date") {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(draft.date)) throw new Error("Укажите дату.");
    const parsed = new Date(`${draft.date}T12:00:00Z`);
    if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== draft.date) {
      throw new Error("Проверьте дату.");
    }
    if (original.mode !== "date" || draft.date !== original.date) patch.date = draft.date;
    return patch;
  }
  const sameStart = original.mode === "time" && draft.date === original.date && draft.time === original.time;
  const start = sameStart && event.datetime ? event.datetime : localInstant(draft.date, draft.time);
  if (!sameStart) patch.datetime = start;
  if (draft.range && event.event_type !== "reminder") {
    const sameEnd = original.range && draft.endDate === original.endDate && draft.endTime === original.endTime;
    const end = sameEnd && event.end_time ? event.end_time : localInstant(draft.endDate, draft.endTime);
    if (new Date(end) <= new Date(start)) throw new Error("Окончание должно быть позже начала.");
    // Passing the displayed end explicitly avoids an implicit duration shift when editing the start.
    if (!sameStart || !sameEnd) patch.end_time = end;
  } else if (event.end_time) patch.clear_end_time = true;
  return patch;
}
