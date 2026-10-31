import { useEffect, useRef, useState } from "react";
import { formatMoment } from "./formatMoment";
import type { EventRecord, TaskStatus } from "./types";

interface EventListProps {
  events: EventRecord[];
  onStatusChange: (id: string, status: TaskStatus) => void;
  onDelete: (id: string) => void;
  onEdit: (event: EventRecord) => void;
  deletingIds: ReadonlySet<string>;
  updatingIds: ReadonlySet<string>;
}
const STATUS_LABELS: Record<TaskStatus, string> = { todo: "Не начата", in_progress: "В работе", done: "Выполнена" };
const TYPE_ICON: Record<EventRecord["event_type"], string> = { task: "📋", meeting: "🤝", reminder: "⏰" };

export function EventList({ events, onStatusChange, onDelete, onEdit, deletingIds, updatingIds }: EventListProps) {
  const [menuId, setMenuId] = useState<string | null>(null);
  if (events.length === 0) return <div className="empty-state"><span className="empty-icon">🗂️</span><p>Здесь пока пусто</p></div>;
  return <ul className="event-list">{events.map((event) => <EventCard key={event.id} event={event}
    open={menuId === event.id} onMenu={setMenuId} onDelete={onDelete} onEdit={onEdit}
    onStatusChange={onStatusChange} busy={deletingIds.has(event.id) || updatingIds.has(event.id)} />)}</ul>;
}
interface CardProps {
  event: EventRecord;
  open: boolean;
  busy: boolean;
  onMenu: (id: string | null) => void;
  onEdit: (event: EventRecord) => void;
  onDelete: (id: string) => void;
  onStatusChange: (id: string, status: TaskStatus) => void;
}
function EventCard({ event, open, busy, onMenu, onEdit, onDelete, onStatusChange }: CardProps) {
  const actions = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const outside = (e: PointerEvent) => {
      if (!actions.current?.contains(e.target as Node)) onMenu(null);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open, onMenu]);
  function closeWithFocus() { onMenu(null); trigger.current?.focus(); }
  return <li className="event-row"><div className="event-card" aria-busy={busy}>
    <div className="event-card-main">
      <span className="event-icon">{TYPE_ICON[event.event_type]}</span>
      <div className="event-text"><span className="event-name">{event.name}</span>
        <span className="event-moment">{formatMoment(event)}</span></div>
      <div ref={actions} className="event-actions" onKeyDown={(e) => {
        if (e.key === "Escape") { e.preventDefault(); closeWithFocus(); }
        if (e.key === "Tab") onMenu(null);
      }}>
        <button ref={trigger} id={`event-actions-${event.id}`} className="event-menu-trigger" type="button" disabled={busy}
          aria-label={`Действия: ${event.name}`} aria-haspopup="menu" aria-expanded={open}
          onClick={() => onMenu(open ? null : event.id)}
          onKeyDown={(e) => { if (e.key === "ArrowDown") { e.preventDefault(); onMenu(event.id); } }}>⋯</button>
        {open && <div ref={menu} role="menu" aria-label={`Действия с записью ${event.name}`} className="event-menu"
          onKeyDown={(e) => {
            if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
            e.preventDefault();
            const items = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
            const current = items.indexOf(document.activeElement as HTMLButtonElement);
            const index = e.key === "Home" ? 0 : e.key === "End" ? items.length - 1 :
              (current + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
            items[index]?.focus();
          }}>
          <button type="button" role="menuitem" disabled={busy} onClick={() => { onMenu(null); onEdit(event); }}>Редактировать</button>
          <button type="button" role="menuitem" className="event-menu-delete" disabled={busy}
            onClick={() => { onMenu(null); onDelete(event.id); }}>Удалить</button>
        </div>}
      </div>
    </div>
    {event.event_type === "task" && event.status && <select className={`status-select status-${event.status}`}
      value={event.status} disabled={busy} aria-label={`Статус задачи: ${event.name}`}
      onChange={(e) => onStatusChange(event.id, e.target.value as TaskStatus)}>
      {(Object.keys(STATUS_LABELS) as TaskStatus[]).map((status) => <option key={status} value={status}>{STATUS_LABELS[status]}</option>)}
    </select>}
  </div></li>;
}
