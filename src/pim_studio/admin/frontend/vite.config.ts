import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'node:fs'
import path from 'node:path'

/**
 * Clear what the build owns, and only that.
 *
 * `emptyOutDir` wipes the whole of `../static`, which also holds `demo/` —
 * the generated data extracts the running app serves at `/demo/*`. So a plain
 * `npm run build` deleted them, and every page then rendered its own
 * "nothing has been generated yet" empty state. That looks exactly like a
 * sync that has never run, which is the most expensive way for it to fail.
 *
 * Production never hit it (there `static/demo` is a bind mount and the build
 * happens at image-build time), which is precisely why it survived: it only
 * breaks the person developing locally, silently, after a successful build.
 */
const OWNED = ['assets', 'index.html']

function clearBuildOutput(outDir: string) {
  return {
    name: 'clear-build-output',
    apply: 'build' as const,
    buildStart() {
      for (const entry of OWNED) {
        fs.rmSync(path.resolve(__dirname, outDir, entry), {
          recursive: true,
          force: true,
        })
      }
    },
  }
}

// Build output goes to the admin/static directory, which FastAPI mounts.
// That keeps the Python package self-contained — no cross-directory reach.
export default defineConfig({
  plugins: [react(), clearBuildOutput('../static')],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: '../static',
    // Off, because it does not mean "empty the build output" — it means empty
    // the directory, and `demo/` lives there too. `clearBuildOutput` above
    // removes the hashed assets so stale bundles still cannot accumulate.
    emptyOutDir: false,
  },
  server: {
    port: 5173,
    proxy: {
      // Dev-only: forward API calls to the FastAPI backend at :8000 so the
      // frontend can fetch real data during development without CORS setup.
      '/api': 'http://localhost:8000',
      // The generated extracts (and the demo's thumbnails) are served by the
      // backend from `static/demo/`. Without this the dev server answers
      // `/demo/*.json` with its own index.html, and the pages fall back to
      // the hand-built fixtures without saying so.
      '/demo': 'http://localhost:8000',
    },
  },
})
