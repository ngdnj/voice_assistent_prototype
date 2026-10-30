import type { EventType } from "./types";

interface TabsProps {
  active: EventType;
  onChange: (type: EventType) => void;
}

const TAB_LABELS: Record<EventType, string> = {
  task: "Задачи",
  meeting: "Встречи",
  reminder: "Напоминания",
};

export function Tabs({ active, onChange }: TabsProps) {
  return (
    <div className="tabs">
      {(Object.keys(TAB_LABELS) as EventType[]).map((type) => (
        <button
          key={type}
          className={type === active ? "tab tab-active" : "tab"}
          onClick={() => onChange(type)}
        >
          {TAB_LABELS[type]}
        </button>
      ))}
    </div>
  );
}