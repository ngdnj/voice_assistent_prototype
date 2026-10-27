import type { EventRecord } from "./types";

// Тестовые данные для разработки интерфейса; заменим на запросы к REST API.
export const mockEvents: EventRecord[] = [
  {
    id: "1",
    event_type: "task",
    name: "Отправить отчёт",
    description: "",
    status: "todo",
    date: "2026-10-28",
    datetime: null,
    end_time: null,
    created_at: "2026-10-27T10:00:00Z",
  },
  {
    id: "2",
    event_type: "task",
    name: "Купить билеты",
    description: "До конца недели",
    status: "in_progress",
    date: "2026-10-30",
    datetime: null,
    end_time: null,
    created_at: "2026-10-27T11:00:00Z",
  },
  {
    id: "3",
    event_type: "meeting",
    name: "Встреча с командой",
    description: "",
    status: null,
    date: null,
    datetime: "2026-10-28T15:00:00Z",
    end_time: "2026-10-28T16:00:00Z",
    created_at: "2026-10-27T09:00:00Z",
  },
  {
    id: "4",
    event_type: "reminder",
    name: "Позвонить Ивану",
    description: "",
    status: null,
    date: null,
    datetime: "2026-10-28T07:00:00Z",
    end_time: null,
    created_at: "2026-10-27T08:00:00Z",
  },
];