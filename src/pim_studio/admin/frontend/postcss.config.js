import { fileURLToPath } from 'node:url'

/**
 * Tailwind looks for its config from the process working directory, not from
 * this file. So `vite src/pim_studio/admin/frontend` started at the repo root
 * — which is how `.claude/launch.json` starts it — found no config, fell back
 * to an empty `content`, and emitted a stylesheet with no utilities in it at
 * all. The app then rendered as unstyled HTML, and the one line saying why
 * scrolled past in the vite log among the proxy errors.
 *
 * Point at the config sitting next to this file. Which directory the server
 * happens to be started from is not something the styling should depend on.
 */
export default {
  plugins: {
    tailwindcss: {
      config: fileURLToPath(new URL('./tailwind.config.ts', import.meta.url)),
    },
    autoprefixer: {},
  },
}
