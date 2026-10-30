import type { EventRecord } from "./types";

const dayFormat = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "long",
  year: "numeric",
});
const timeFormat = new Intl.DateTimeFormat("ru-RU", {
  hour: "2-digit",
  minute: "2-digit",
});

function sameLocalDay(first: Date, second: Date): boolean {
  return first.getFullYear() === second.getFullYear()
    && first.getMonth() === second.getMonth()
    && first.getDate() === second.getDate();
}

export function formatMoment(event: EventRecord): string {
  if (event.datetime) {
    const start = new Date(event.datetime);
    if (Number.isNaN(start.getTime())) return "Время не указано";
    const beginning = `${dayFormat.format(start)}, ${timeFormat.format(start)}`;
    if (event.end_time && event.event_type !== "reminder") {
      const end = new Date(event.end_time);
      if (!Number.isNaN(end.getTime())) {
        return sameLocalDay(start, end)
          ? `${beginning}–${timeFormat.format(end)}`
          : `${beginning} — ${dayFormat.format(end)}, ${timeFormat.format(end)}`;
      }
    }
    return beginning;
  }
  if (event.date) {
    // DATE is a local calendar date; new Date("YYYY-MM-DD") would interpret it as UTC.
    const [year, month, day] = event.date.split("-").map(Number);
    const date = new Date(year, month - 1, day);
    return Number.isNaN(date.getTime()) ? "Дата не указана" : `${dayFormat.format(date)}, без времени`;
  }
  return "Без срока";
}
