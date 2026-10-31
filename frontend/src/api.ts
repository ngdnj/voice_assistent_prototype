import { buildTestInitData } from "./testAuth";
import type { EventRecord, TaskStatus } from "./types";
import type { EventPatch } from "./eventEditing";

// Mini App и REST работают на одном домене: Caddy отдаёт сайт и проксирует /api.
// При `npm run dev` запросы /api проксирует Vite (vite.config.ts).
const API_BASE = "/api";

// MAX Bridge (https://dev.max.ru/docs/webapps/bridge) создаёт window.WebApp при запуске из MAX.
declare global {
  interface Window {
    WebApp?: { initData?: string };
  }
}

// initData недействительна или приложение открыто не из MAX: нужно открыть его из бота заново.
export class UnauthorizedError extends Error {}

async function authHeaders(): Promise<HeadersInit> {
  const initData = window.WebApp?.initData;
  if (initData) {
    return { "X-Max-Init-Data": initData };
  }
  // Тестовая подпись только для локальной разработки; в собранном приложении её нет.
  if (import.meta.env.DEV) {
    return { "X-Max-Init-Data": await buildTestInitData() };
  }
  throw new UnauthorizedError("Откройте приложение из MAX");
}

async function readBody(response: Response, fallback: string): Promise<Record<string, unknown>> {
  if (response.status === 401) {
    throw new UnauthorizedError("Откройте приложение из MAX заново");
  }
  let body: { error?: { message?: string } } & Record<string, unknown>;
  try {
    body = await response.json();
  } catch {
    throw new Error(fallback);
  }
  if (!response.ok) {
    throw new Error(body.error?.message ?? fallback);
  }
  return body;
}

export async function fetchEvents(startTime: string, endTime: string): Promise<EventRecord[]> {
  const headers = await authHeaders();
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const params = new URLSearchParams({ start_time: startTime, end_time: endTime, timezone });

  const response = await fetch(`${API_BASE}/events?${params}`, { headers });
  const body = await readBody(response, "Не удалось загрузить записи");
  return body.events as EventRecord[];
}

export async function updateTaskStatus(id: string, status: TaskStatus): Promise<EventRecord> {
  const headers = await authHeaders();
  const response = await fetch(`${API_BASE}/tasks/${encodeURIComponent(id)}/status`, {
    method: "PATCH",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  const body = await readBody(response, "Не удалось изменить статус");
  return recordResult(body, "event", id);
}

function recordResult(body: Record<string, unknown>, field: string, id: string): EventRecord {
  const record = body[field];
  if (!record || typeof record !== "object" || !("id" in record) || record.id !== id) {
    throw new Error("Сервер не подтвердил изменение записи");
  }
  return record as EventRecord;
}

export async function deleteEvent(id: string, operationId: string): Promise<EventRecord> {
  const headers = await authHeaders();
  const response = await fetch(`${API_BASE}/events/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: { ...headers, "X-Action-Id": operationId },
  });
  const body = await readBody(response, "Не удалось удалить запись");
  return recordResult(body, "deleted", id);
}

export async function updateEvent(id: string, changes: EventPatch, operationId: string): Promise<EventRecord> {
  const headers = await authHeaders();
  const params = new URLSearchParams({ timezone: Intl.DateTimeFormat().resolvedOptions().timeZone });
  const response = await fetch(`${API_BASE}/events/${encodeURIComponent(id)}?${params}`, {
    method: "PATCH",
    headers: { ...headers, "Content-Type": "application/json", "X-Action-Id": operationId },
    body: JSON.stringify(changes),
  });
  return recordResult(await readBody(response, "Не удалось сохранить изменения"), "updated", id);
}

export async function searchEvents(query: string, eventType: string): Promise<EventRecord[]> {
  const headers = await authHeaders();
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const params = new URLSearchParams({ query, timezone });
  params.append("event_types", eventType);

  const response = await fetch(`${API_BASE}/events/search?${params}`, { headers });
  const body = await readBody(response, "Не удалось найти записи");
  return body.events as EventRecord[];
}
