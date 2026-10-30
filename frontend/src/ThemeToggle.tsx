import type { Theme } from "./theme";

interface ThemeToggleProps {
  theme: Theme;
  onToggle: () => void;
}

// В тёмной теме кнопка показывает солнце (переключит на светлую), в светлой — луну.
export function ThemeToggle({ theme, onToggle }: ThemeToggleProps) {
  const label = theme === "dark" ? "Включить светлую тему" : "Включить тёмную тему";
  return (
    <button type="button" className="theme-toggle" onClick={onToggle} aria-label={label} title={label}>
      {theme === "dark" ? (
        <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
          <circle cx="12" cy="12" r="4.5" fill="currentColor" />
          <g stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <line x1="12" y1="1.5" x2="12" y2="4" />
            <line x1="12" y1="20" x2="12" y2="22.5" />
            <line x1="1.5" y1="12" x2="4" y2="12" />
            <line x1="20" y1="12" x2="22.5" y2="12" />
            <line x1="4.6" y1="4.6" x2="6.4" y2="6.4" />
            <line x1="17.6" y1="17.6" x2="19.4" y2="19.4" />
            <line x1="4.6" y1="19.4" x2="6.4" y2="17.6" />
            <line x1="17.6" y1="6.4" x2="19.4" y2="4.6" />
          </g>
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
          <path
            fill="currentColor"
            d="M20.5 14.6A8.5 8.5 0 0 1 9.4 3.5a.6.6 0 0 0-.8-.7A9.5 9.5 0 1 0 21.2 15.4a.6.6 0 0 0-.7-.8z"
          />
        </svg>
      )}
    </button>
  );
}
