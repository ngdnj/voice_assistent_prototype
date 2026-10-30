# Mini App: записи, интервалы и удаление

Задачи и встречи показывают начало и окончание интервала в локальной зоне браузера.
Если интервал переходит на другой день, показаны обе даты. Срок задачи без времени
остаётся календарной датой и подписан «без времени».

Удерживайте карточку около половины секунды: слева появится кнопка с корзиной.
Нажатие на корзину удаляет запись. Само удерживание ничего не удаляет; движение
пальца, прокрутка и отпускание до окончания удерживания отменяют жест. С клавиатуры:
фокус на карточке → Enter/Space → кнопка удаления; Escape скрывает кнопку.

Удаление вызывает `DELETE /api/events/{id}` с проверенной MAX initData и UUID в
`X-Action-Id`. Повтор после ошибки использует тот же UUID. Карточка исчезает после
подтверждения сервера; во время запроса изменение статуса и повторное удаление
заблокированы. Ошибка показывается над списком, запись остаётся на экране.

Из корня `frontend/`:

```bash
npm ci
npm run build
npm run lint
npm test
# Дополнительно: локально установленные Chromium и chromedriver, без настоящего MAX/API.
node tests/browser-smoke.mjs
```

Тесты используют встроенный Node test runner и нативное чтение TypeScript,
проверены на Node 26.12. Browser smoke проверяет реальный собранный интерфейс с
подставным HTTP API: удерживание мыши/касание, отмену жеста, расположение корзины,
клавиатуру, ошибки, стабильный UUID повтора, отсутствие восстановления удалённой
записи устаревшим GET и интервалы. Пути браузера можно задать через `CHROME_BIN` и
`CHROMEDRIVER`.

## Исходные сведения шаблона React + TypeScript + Vite

This template provides a minimal setup to get React working in Vite with HMR and some ESLint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend updating the configuration to enable type-aware lint rules:

```js
export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...

      // Remove tseslint.configs.recommended and replace with this
      tseslint.configs.recommendedTypeChecked,
      // Alternatively, use this for stricter rules
      tseslint.configs.strictTypeChecked,
      // Optionally, add this for stylistic rules
      tseslint.configs.stylisticTypeChecked,

      // Other configs...
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])

```

You can also install [eslint-plugin-react-x](https://npmx.dev/package/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://npmx.dev/package/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from 'eslint-plugin-react-x'
import reactDom from 'eslint-plugin-react-dom'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...
      // Enable lint rules for React
      reactX.configs['recommended-typescript'],
      // Enable lint rules for React DOM
      reactDom.configs.recommended,
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])

```
