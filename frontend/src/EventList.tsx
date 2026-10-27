import type { EventRecord, TaskStatus } from "./types";

interface EventListProps {
  events: EventRecord[];
  onStatusChange: (id: string, status: TaskStatus) => void;
}

const STATUS_LABELS: Record<TaskStatus, string> = {
  todo: "Не начата",
  in_progress: "В работе",
  done: "Выполнена",
};

function formatMoment(event: EventRecord): string {
  if (event.datetime) {
    return new Date(event.datetime).toLocaleString("ru-RU", {
      day: "numeric",
      month: "long",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  if (event.date) {
    return new Date(event.date).toLocaleDateString("ru-RU", {
      day: "numeric",
      month: "long",
    });
  }
  return "Без срока";
}

export function EventList({ events, onStatusChange }: EventListProps) {
  if (events.length === 0) {
    return <p className="empty-state">Записей нет</p>;
  }

  return (
    <ul className="event-list">
      {events.map((event) => (
        <li key={event.id} className="event-card">
          <div className="event-card-main">
            <span className="event-name">{event.name}</span>
            <span className="event-moment">{formatMoment(event)}</span>
          </div>
          {event.event_type === "task" && event.status && (
            <select
              value={event.status}
              onChange={(e) =>
                onStatusChange(event.id, e.target.value as TaskStatus)
              }
            >
              {(Object.keys(STATUS_LABELS) as TaskStatus[]).map((status) => (
                <option key={status} value={status}>
                  {STATUS_LABELS[status]}
                </option>
              ))}
            </select>
          )}
        </li>
      ))}
    </ul>
  );
}