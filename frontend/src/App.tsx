import { useState } from "react";
import { Tabs } from "./Tabs";
import { EventList } from "./EventList";
import { mockEvents } from "./mockData";
import "./App.css";
import type { EventType, TaskStatus } from "./types";

function App() {
  const [activeTab, setActiveTab] = useState<EventType>("task");
  const [events, setEvents] = useState(mockEvents);

  const visibleEvents = events.filter((event) => event.event_type === activeTab);

  function handleStatusChange(id: string, status: TaskStatus) {
    setEvents((prev) =>
      prev.map((event) => (event.id === id ? { ...event, status } : event))
    );
  }

  return (
    <div className="app">
      <h1>Мои записи</h1>
      <Tabs active={activeTab} onChange={setActiveTab} />
      <EventList events={visibleEvents} onStatusChange={handleStatusChange} />
    </div>
  );
}

export default App;