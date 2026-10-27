import { useEffect, useState } from "react";
import "./App.css";
import { Tabs } from "./Tabs";
import { EventList } from "./EventList";
import { mockEvents } from "./mockData";
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

  // Пока нет настоящего API — имитируем загрузку по периоду на тестовых данных.
  useEffect(() => {
    setStatus("loading");
    const timer = setTimeout(() => {
      try {
        const start = new Date(from);
        const end = new Date(to);
        end.setDate(end.getDate() + 1); // конец периода включительно
        const filtered = mockEvents.filter((event) => {
          const moment = event.datetime ?? event.date;
          if (!moment) return true; // без срока — показываем всегда
          const momentDate = new Date(moment);
          return momentDate >= start && momentDate < end;
        });
        setEvents(filtered);
        setStatus("ready");
      } catch {
        setStatus("error");
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [from, to]);

  const visibleEvents = events.filter((event) => event.event_type === activeTab);

  function handleStatusChange(id: string, status: TaskStatus) {
    setEvents((prev) =>
      prev.map((event) => (event.id === id ? { ...event, status } : event))
    );
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