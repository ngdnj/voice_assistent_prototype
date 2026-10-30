// Тип записи: совпадает с event_type на бэкенде (app/mcp_server/schemas.py)
export type EventType = "task" | "meeting" | "reminder";

export type TaskStatus = "todo" | "in_progress" | "done";

// Одна запись — ровно те поля, что отдаёт бэкенд в EventDTO.
// У задачи заполнены status и date/datetime; интервал задачи/встречи — datetime и end_time;
// у напоминания — только datetime. Неиспользуемые поля приходят как null.
export interface EventRecord {
  id: string;
  event_type: EventType;
  name: string;
  description: string;
  status: TaskStatus | null;
  date: string | null;
  datetime: string | null;
  end_time: string | null;
  created_at: string;
}
