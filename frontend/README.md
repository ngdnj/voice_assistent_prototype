# Mini App: редактирование и удаление записей

На каждой карточке постоянно видна кнопка «⋯». Она открывает меню «Редактировать»
и «Удалить»; удерживать карточку больше не нужно. Enter/Space открывают меню,
стрелки переключают действия, Escape и нажатие вне меню закрывают его.

Редактор меняет название, дату и время задач, встреч и напоминаний. У задачи
доступны «Без срока», «Только дата», «Дата и время». Для задачи или встречи можно
задать/убрать окончание интервала. Встреча и напоминание требуют точного времени;
длительность не придумывается. Дата без времени остаётся DATE, не полуночью UTC.
Время отображается и вводится в зоне устройства. Редактор использует 24-часовой
формат `ЧЧ:ММ` (`00:00–23:59`) без AM/PM независимо от локали браузера.
При переходах часов несуществующее
или неоднозначное местное время отклоняется; уже сохранённый точный момент при
простом переименовании не меняется, включая секунды и явное смещение.

Сохранение вызывает `PATCH /api/events/{id}?timezone=...`, удаление —
`DELETE /api/events/{id}`. Владелец проверяется по MAX initData, UUID операции
передаётся в `X-Action-Id`. Тот же набор изменений после ошибки сохраняет UUID;
новые данные получают новый UUID. Изменения и удаление одной карточки не запускаются
одновременно. Ответ с чужим ID не принимается как успех.

Ошибка сохранения остаётся в редакторе, введённые значения не теряются. После
успеха показывается «Изменения сохранены», обновляется список с текущим периодом
или поиском. Задача без срока может исчезнуть из периода — её можно найти поиском.
Устаревший GET не откатывает изменение и не возвращает удалённую карточку.

Из `frontend/`:

```bash
npm ci
npm run build
npm run lint
npm test
# Chromium и chromedriver должны быть установлены локально; настоящий MAX/API не вызываются.
node tests/browser-smoke.mjs
```

Node test runner проверяет формирование PATCH, UTC/локальные даты, интервалы,
переходы часов и дату без времени. Browser smoke работает с настоящим собранным
React-интерфейсом и подставным HTTP API: меню, клавиатура/касание, форма, повтор
после неопределённого результата, stale GET, период/поиск и удаление всех типов.
Пути браузера задаются через `CHROME_BIN` и `CHROMEDRIVER`.

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
