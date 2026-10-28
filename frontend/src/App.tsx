import { useEffect, useState } from "react";
import "./App.css";
import { Tabs } from "./Tabs";
import { EventList } from "./EventList";
import { fetchEvents, updateTaskStatus } from "./api";
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

  useEffect(() => {
    let cancelled = false;
    setStatus("loading");

    const start = new Date(from).toISOString();
    const endDate = new Date(to);
    endDate.setDate(endDate.getDate() + 1); // конец периода включительно
    const end = endDate.toISOString();

    fetchEvents(start, end)
      .then((data) => {
        if (!cancelled) {
          setEvents(data);
          setStatus("ready");
        }
      })
      .catch(() => {
        if (!cancelled) setStatus("error");
      });

    return () => {
      cancelled = true;
    };
  }, [from, to]);

  const visibleEvents = events.filter((event) => event.event_type === activeTab);

  async function handleStatusChange(id: string, newStatus: TaskStatus) {
    const previous = events;
    setEvents((prev) =>
      prev.map((event) => (event.id === id ? { ...event, status: newStatus } : event))
    );
    try {
      await updateTaskStatus(id, newStatus);
    } catch {
      setEvents(previous); // откатываем, если backend отклонил изменение
    }
  }

  return (
    <div className="app">
      <h1>Мои записи</h1>

      <div className="period">
        <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        <span>—</span>
        <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
      </div>

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