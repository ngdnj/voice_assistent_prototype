import { useEffect, useMemo, useRef, useState } from "react";
import { formatMoment } from "./formatMoment";
import { createLongPress } from "./longPress";
import type { EventRecord, TaskStatus } from "./types";

interface EventListProps {
  events: EventRecord[];
  onStatusChange: (id: string, status: TaskStatus) => void;
  onDelete: (id: string) => void;
  deletingIds: ReadonlySet<string>;
  updatingIds: ReadonlySet<string>;
}

const STATUS_LABELS: Record<TaskStatus, string> = {
  todo: "Не начата",
  in_progress: "В работе",
  done: "Выполнена",
};

const TYPE_ICON: Record<EventRecord["event_type"], string> = {
  task: "📋",
  meeting: "🤝",
  reminder: "⏰",
};

export function EventList({ events, onStatusChange, onDelete, deletingIds, updatingIds }: EventListProps) {
  const [revealedId, setRevealedId] = useState<string | null>(null);
  if (events.length === 0) {
    return (
      <div className="empty-state">
        <span className="empty-icon">🗂️</span>
        <p>Здесь пока пусто</p>
      </div>
    );
  }

  return <>
    <p className="event-list-help" id="event-list-help">
      Удерживайте запись, чтобы удалить.
      <span className="visually-hidden">С клавиатуры: Enter — показать удаление, Escape — скрыть.</span>
    </p>
    <ul className="event-list">
      {events.map((event) => (
        <EventCard key={event.id} event={event} revealed={revealedId === event.id}
          onReveal={setRevealedId} onDelete={onDelete} onStatusChange={onStatusChange}
          deleting={deletingIds.has(event.id)} updating={updatingIds.has(event.id)} />
      ))}
    </ul>
  </>;
}

interface EventCardProps {
  event: EventRecord;
  revealed: boolean;
  onReveal: (id: string | null) => void;
  onDelete: (id: string) => void;
  onStatusChange: (id: string, status: TaskStatus) => void;
  deleting: boolean;
  updating: boolean;
}

function EventCard({ event, revealed, onReveal, onDelete, onStatusChange, deleting, updating }: EventCardProps) {
  const press = useMemo(() => createLongPress(() => onReveal(event.id)), [event.id, onReveal]);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const focusDelete = useRef(false);
  useEffect(() => {
    if (revealed && focusDelete.current) {
      deleteButton.current?.focus();
      focusDelete.current = false;
    }
  }, [revealed]);
  useEffect(() => {
    const cancel = () => press.cancel();
    window.addEventListener("scroll", cancel, true);
    window.addEventListener("blur", cancel);
    return () => {
      press.cancel();
      window.removeEventListener("scroll", cancel, true);
      window.removeEventListener("blur", cancel);
    };
  }, [press]);
  const busy = deleting || updating;

  return <li className={`event-row${revealed ? " event-row-revealed" : ""}`}
    onKeyDown={(e) => {
      if (e.key === "Escape" && revealed) {
        onReveal(null);
        card.current?.focus();
      }
    }}>
    {revealed && <button ref={deleteButton} type="button" className="event-delete" disabled={busy}
      aria-label={deleting ? `Удаляется: ${event.name}` : `Удалить: ${event.name}`}
      title="Удалить запись" onClick={() => onDelete(event.id)}>
      {deleting ? <span aria-hidden="true">…</span> : <svg viewBox="0 0 24 24" width="22" height="22"
        fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7" />
      </svg>}
    </button>}
    <div ref={card} className="event-card" tabIndex={0} aria-describedby="event-list-help"
      aria-busy={busy}
      onPointerDown={(e) => {
        if (!e.isPrimary || e.button !== 0 || busy) return;
        if ((e.target as Element).closest("button, select, input, a")) return;
        press.start(e.pointerId, e.clientX, e.clientY);
      }}
      onPointerMove={(e) => press.move(e.pointerId, e.clientX, e.clientY)}
      onPointerUp={(e) => press.cancel(e.pointerId)}
      onPointerCancel={(e) => press.cancel(e.pointerId)}
      onPointerLeave={() => press.cancel()}
      onContextMenu={(e) => { e.preventDefault(); if (!busy) onReveal(event.id); }}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if ((e.key === "Enter" || e.key === " ") && !busy) {
          e.preventDefault();
          focusDelete.current = !revealed;
          onReveal(revealed ? null : event.id);
        }
      }}>
          <div className="event-card-main">
            <span className="event-icon">{TYPE_ICON[event.event_type]}</span>
            <div className="event-text">
              <span className="event-name">{event.name}</span>
              <span className="event-moment">{formatMoment(event)}</span>
            </div>
          </div>
          {event.event_type === "task" && event.status && (
            <select
              className={`status-select status-${event.status}`}
              value={event.status}
              disabled={busy}
              aria-label={`Статус задачи: ${event.name}`}
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
    </div>
  </li>;
}
