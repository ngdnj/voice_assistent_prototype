import { buildTestInitData } from "./testAuth";
import type { EventRecord, TaskStatus } from "./types";

const API_BASE = "http://127.0.0.1:8001/api";

async function authHeaders(): Promise<HeadersInit> {
  const initData = await buildTestInitData();
  return { "X-Max-Init-Data": initData };
}

export async function fetchEvents(startTime: string, endTime: string): Promise<EventRecord[]> {
  const headers = await authHeaders();
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const params = new URLSearchParams({ start_time: startTime, end_time: endTime, timezone });

  const response = await fetch(`${API_BASE}/events?${params}`, { headers });
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.error?.message ?? "Не удалось загрузить записи");
  }
  return body.events as EventRecord[];
}

export async function updateTaskStatus(id: string, status: TaskStatus): Promise<void> {
  const headers = await authHeaders();
  const response = await fetch(`${API_BASE}/tasks/${id}/status`, {
    method: "PATCH",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!response.ok) {
    const body = await response.json();
    throw new Error(body.error?.message ?? "Не удалось изменить статус");
  }
}