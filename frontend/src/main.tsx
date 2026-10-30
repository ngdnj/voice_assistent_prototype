import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { applyTheme, initialTheme } from './theme'

// Тема ставится до первой отрисовки, чтобы при запуске не мелькала другая.
const theme = initialTheme()
applyTheme(theme, false)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App initialTheme={theme} />
  </StrictMode>,
)
