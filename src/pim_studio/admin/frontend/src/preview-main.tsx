import React from 'react'
import ReactDOM from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import App from './App'
import { AuthProvider } from './lib/auth'
import './index.css'

/**
 * Dev-only entry for `preview.html`.
 *
 * Renders the **real** app — same route table, same shell, same role gating —
 * with a stand-in user so nothing calls `/api/auth/me` against a backend that
 * doesn't exist yet. Deliberately not a second copy of the routes: a
 * duplicate would drift from the real one and stop being worth looking at.
 *
 * The stand-in is a `developer` so every nav item and admin page is reachable
 * while the design is being reviewed. Not part of the production build —
 * Vite only builds `index.html` unless told otherwise.
 *
 * Routing is hash-based here, unlike production. A path router would put the
 * route after the filename (`/preview.html/products`), and on a hard reload
 * the dev server's SPA fallback serves `index.html` — the real app — which
 * then bounces to the login it has no backend for. Hash routes keep the
 * filename intact, so reloads and deep links land where they should.
 */
const PREVIEW_USER = {
  id: 0,
  email: 'preview@local',
  name: 'Preview',
  role: 'developer' as const,
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <HashRouter>
      <AuthProvider initialUser={PREVIEW_USER}>
        <App />
      </AuthProvider>
    </HashRouter>
  </React.StrictMode>,
)
