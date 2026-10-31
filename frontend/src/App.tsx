import { useEffect, useRef, useState } from "react";
import "./App.css";
import { Tabs } from "./Tabs";
import { EventList } from "./EventList";
import { EventEditor } from "./EventEditor";
import type { EventPatch } from "./eventEditing";
import { ThemeToggle } from "./ThemeToggle";
import { applyTheme, type Theme } from "./theme";
import { deleteEvent, fetchEvents, searchEvents, UnauthorizedError, updateEvent, updateTaskStatus } from "./api";
import type { EventRecord, EventType, TaskStatus } from "./types";

type Status = "loading" | "error" | "unauthorized" | "ready";

// Дата YYYY-MM-DD в локальной зоне пользователя (toISOString дал бы дату по UTC).
function localDate(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

// Начало локальных суток: так период совпадает с тем, как его понимает бот.
function startOfLocalDay(value: string): Date {
  return new Date(`${value}T00:00:00`);
}

function App({ initialTheme }: { initialTheme: Theme }) {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [activeTab, setActiveTab] = useState<EventType>("task");
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [status, setStatus] = useState<Status>("loading");
  const [from, setFrom] = useState(() => localDate(new Date()));
  const [to, setTo] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() + 7);
    return localDate(d);
  });
  const [query, setQuery] = useState("");
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [mutationNotice, setMutationNotice] = useState<string | null>(null);
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());
  const [updatingIds, setUpdatingIds] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<EventRecord | null>(null);
  const [reload, setReload] = useState(0);
  const mutationVersion = useRef(0);
  const deletedIds = useRef(new Set<string>());
  const deleting = useRef(new Set<string>());
  const updating = useRef(new Set<string>());
  // A retry after an uncertain network result must refer to the same logical deletion.
  const deleteOperations = useRef(new Map<string, string>());
  const editOperations = useRef(new Map<string, { payload: string; operationId: string }>());

  useEffect(() => {
    let cancelled = false;
    const version = mutationVersion.current;

    // Поиск по словам (query) не привязан к периоду — он должен находить
    // даже задачи без срока. Если строка поиска пустая — обычная загрузка по периоду.
    const trimmed = query.trim();
    const load = () => {
      if (trimmed) return searchEvents(trimmed, activeTab);
      // Полуинтервал [начало дня from, начало дня после to) в локальной зоне.
      const start = startOfLocalDay(from).toISOString();
      const endDate = startOfLocalDay(to);
      endDate.setDate(endDate.getDate() + 1);
      return fetchEvents(start, endDate.toISOString());
    };

    // Запрос создаётся только после задержки, поэтому отменённый ввод не уходит на сервер.
    // Promise.resolve().then ловит и синхронные ошибки, например пустую дату.
    const timer = setTimeout(() => {
      Promise.resolve()
        .then(load)
        .then((data) => {
          if (!cancelled && version === mutationVersion.current) {
            // An older in-flight GET must not resurrect a successfully deleted record.
            setEvents(data.filter((event) => !deletedIds.current.has(event.id)));
            setStatus("ready");
          }
        })
        .catch((error: unknown) => {
          if (!cancelled && version === mutationVersion.current) setStatus(error instanceof UnauthorizedError ? "unauthorized" : "error");
        });
    }, 300); // небольшая задержка, чтобы не слать запрос на каждую букву

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [from, to, query, activeTab, reload]);

  function refreshAfterMutation() {
    mutationVersion.current += 1;
    setReload((value) => value + 1);
  }

  function closeEditor() {
    const id = editing?.id;
    setEditing(null);
    if (id) requestAnimationFrame(() => document.getElementById(`event-actions-${id}`)?.focus());
  }

  const visibleEvents = query.trim()
    ? events
    : events.filter((event) => event.event_type === activeTab);

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    applyTheme(next);
    setTheme(next);
  }

  async function handleStatusChange(id: string, newStatus: TaskStatus) {
    if (deleting.current.has(id) || updating.current.has(id) || deletedIds.current.has(id)) return;
    updating.current.add(id);
    setUpdatingIds(new Set(updating.current));
    setMutationError(null);
    try {
      const changed = await updateTaskStatus(id, newStatus);
      setEvents((current) => current.map((event) => event.id === id ? changed : event));
      refreshAfterMutation();
    } catch (error: unknown) {
      if (error instanceof UnauthorizedError) setStatus("unauthorized");
      else setMutationError("Не удалось изменить статус. Попробуйте ещё раз.");
    } finally {
      updating.current.delete(id);
      setUpdatingIds(new Set(updating.current));
    }
  }

  async function handleEdit(id: string, changes: EventPatch) {
    if (deleting.current.has(id) || updating.current.has(id) || deletedIds.current.has(id)) {
      throw new Error("Запись уже изменяется. Дождитесь завершения операции.");
    }
    updating.current.add(id);
    setUpdatingIds(new Set(updating.current));
    setMutationError(null);
    setMutationNotice(null);
    try {
      const payload = JSON.stringify(changes);
      let operation = editOperations.current.get(id);
      if (!operation || operation.payload !== payload) {
        operation = { payload, operationId: crypto.randomUUID() };
        editOperations.current.set(id, operation);
      }
      const changed = await updateEvent(id, changes, operation.operationId);
      setEvents((current) => current.map((event) => event.id === id ? changed : event));
      editOperations.current.delete(id);
      closeEditor();
      setMutationNotice("Изменения сохранены.");
      refreshAfterMutation();
    } catch (error: unknown) {
      if (error instanceof UnauthorizedError) { setStatus("unauthorized"); setEditing(null); }
      throw error;
    } finally {
      updating.current.delete(id);
      setUpdatingIds(new Set(updating.current));
    }
  }

  async function handleDelete(id: string) {
    if (deleting.current.has(id) || updating.current.has(id) || deletedIds.current.has(id)) return;
    deleting.current.add(id);
    setDeletingIds(new Set(deleting.current));
    setMutationError(null);
    setMutationNotice(null);
    try {
      let operationId = deleteOperations.current.get(id);
      if (!operationId) {
        operationId = crypto.randomUUID();
        deleteOperations.current.set(id, operationId);
      }
      await deleteEvent(id, operationId);
      deletedIds.current.add(id);
      setEvents((current) => current.filter((event) => event.id !== id));
      refreshAfterMutation();
    } catch (error: unknown) {
      if (error instanceof UnauthorizedError) setStatus("unauthorized");
      else setMutationError("Не удалось удалить запись. Повторите попытку.");
    } finally {
      deleting.current.delete(id);
      setDeletingIds(new Set(deleting.current));
    }
  }

  return (
    <div className="app">
      <ThemeToggle theme={theme} onToggle={toggleTheme} />
      <h1>Мои записи</h1>

      <input
        className="search-input"
        type="text"
        placeholder="Найти запись…"
        value={query}
        onChange={(e) => {
          if (e.target.value !== query) {
            setStatus("loading");
            setQuery(e.target.value);
          }
        }}
      />

      {!query.trim() && (
        <div className="period">
          <input type="date" value={from} onChange={(e) => {
            if (e.target.value !== from) {
              setStatus("loading");
              setFrom(e.target.value);
            }
          }} />
          <span>—</span>
          <input type="date" value={to} onChange={(e) => {
            if (e.target.value !== to) {
              setStatus("loading");
              setTo(e.target.value);
            }
          }} />
        </div>
      )}

      <Tabs active={activeTab} onChange={(tab) => {
        if (tab !== activeTab) {
          setStatus("loading");
          setActiveTab(tab);
        }
      }} />

      {mutationError && <p className="status-error" role="alert">{mutationError}</p>}
      {mutationNotice && <p className="mutation-notice" role="status">{mutationNotice}</p>}

      {status === "loading" && <p className="status-message">Загрузка…</p>}
      {status === "error" && (
        <p className="status-message status-error">
          Не удалось загрузить записи. Попробуйте ещё раз.
        </p>
      )}
      {status === "unauthorized" && (
        <p className="status-message status-error">
          Откройте приложение заново из чата с ботом в MAX.
        </p>
      )}
      {status === "ready" && (
        <EventList events={visibleEvents} onStatusChange={handleStatusChange}
          onDelete={handleDelete} onEdit={setEditing} deletingIds={deletingIds} updatingIds={updatingIds} />
      )}
      {editing && <EventEditor key={editing.id} event={editing} saving={updatingIds.has(editing.id)}
        onSave={handleEdit} onClose={closeEditor} />}
    </div>
  );
}

export default App;
