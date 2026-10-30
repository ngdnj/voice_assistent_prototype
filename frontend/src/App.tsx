import { useEffect, useState } from "react";
import "./App.css";
import { Tabs } from "./Tabs";
import { EventList } from "./EventList";
import { ThemeToggle } from "./ThemeToggle";
import { applyTheme, type Theme } from "./theme";
import { fetchEvents, searchEvents, UnauthorizedError, updateTaskStatus } from "./api";
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

  useEffect(() => {
    let cancelled = false;
    setStatus("loading");

    // Поиск по смыслу (query) не привязан к периоду — он должен находить
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
          if (!cancelled) {
            setEvents(data);
            setStatus("ready");
          }
        })
        .catch((error: unknown) => {
          if (!cancelled) setStatus(error instanceof UnauthorizedError ? "unauthorized" : "error");
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

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    applyTheme(next);
    setTheme(next);
  }

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
      <ThemeToggle theme={theme} onToggle={toggleTheme} />
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
      {status === "unauthorized" && (
        <p className="status-message status-error">
          Откройте приложение заново из чата с ботом в MAX.
        </p>
      )}
      {status === "ready" && (
        <EventList events={visibleEvents} onStatusChange={handleStatusChange} />
      )}
    </div>
  );
}

export default App;