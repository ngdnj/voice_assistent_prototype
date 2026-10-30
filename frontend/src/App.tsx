import { useEffect, useState } from "react";
import "./App.css";
import { Tabs } from "./Tabs";
import { EventList } from "./EventList";
import { fetchEvents, searchEvents, updateTaskStatus } from "./api";
import type { EventRecord, EventType, TaskStatus } from "./types";

type Status = "loading" | "error" | "ready";

function App() {
  const [activeTab, setActiveTab] = useState<EventType>("task");
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [status, setStatus] = useState<Status>("loading");
  const [from, setFrom] = useState(() => new Date().toISOString().slice(0, 10));
  const [to, setTo] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() + 7);
    return d.toISOString().slice(0, 10);
  });
  const [query, setQuery] = useState("");

  useEffect(() => {
    let cancelled = false;
    setStatus("loading");

    // Поиск по смыслу (query) не привязан к периоду — он должен находить
    // даже задачи без срока. Если строка поиска пустая — обычная загрузка по периоду.
    const trimmed = query.trim();
    const request = trimmed
      ? searchEvents(trimmed, activeTab)
      : (() => {
          const start = new Date(from).toISOString();
          const endDate = new Date(to);
          endDate.setDate(endDate.getDate() + 1);
          return fetchEvents(start, endDate.toISOString());
        })();

    const timer = setTimeout(() => {
      request
        .then((data) => {
          if (!cancelled) {
            setEvents(data);
            setStatus("ready");
          }
        })
        .catch(() => {
          if (!cancelled) setStatus("error");
        });
    }, 300); // небольшая задержка, чтобы не слать запрос на каждую букву

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [from, to, query, activeTab]);

  const visibleEvents = query.trim()
    ? events
    : events.filter((event) => event.event_type === activeTab);

  async function handleStatusChange(id: string, newStatus: TaskStatus) {
    const previous = events;
    setEvents((prev) =>
      prev.map((event) => (event.id === id ? { ...event, status: newStatus } : event))
    );
    try {
      await updateTaskStatus(id, newStatus);
    } catch {
      setEvents(previous);
    }
  }

  return (
    <div className="app">
      <h1>Мои записи</h1>

      <input
        className="search-input"
        type="text"
        placeholder="Найти запись…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      {!query.trim() && (
        <div className="period">
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          <span>—</span>
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
        </div>
      )}

      <Tabs active={activeTab} onChange={setActiveTab} />

      {status === "loading" && <p className="status-message">Загрузка…</p>}
      {status === "error" && (
        <p className="status-message status-error">
          Не удалось загрузить записи. Попробуйте ещё раз.
        </p>
      )}
      {status === "ready" && (
        <EventList events={visibleEvents} onStatusChange={handleStatusChange} />
      )}
    </div>
  );
}

export default App;