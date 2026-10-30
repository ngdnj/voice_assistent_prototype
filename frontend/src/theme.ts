export type Theme = "dark" | "light";

const STORAGE_KEY = "mini-app-theme";

// Выбор пользователя запоминается в браузере; без него берётся тема системы.
// localStorage может быть недоступен (приватный режим, webview), поэтому ошибки не критичны.
export function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "dark" || saved === "light") return saved;
  } catch {
    // Нет доступа к хранилищу — используем тему системы.
  }
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

// remember=false при запуске: пока пользователь не нажал кнопку, следуем теме системы.
export function applyTheme(theme: Theme, remember = true): void {
  document.documentElement.dataset.theme = theme;
  if (!remember) return;
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Тема всё равно применена, просто не сохранится до следующего запуска.
  }
}
