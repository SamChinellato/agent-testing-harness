# Dashboard UI

React and TypeScript, built with Vite. The UI is served by the dashboard API from `dist/`.

```bash
npm install
npm run dev          # hot reload; proxies /api and /metrics to the dashboard on :8000
npm run build        # type-check, then build to dist/
npm test             # unit tests (Vitest)
npm run test:e2e     # end-to-end (Playwright) against a real backend
```

Set `HARNESS_BACKEND=http://host:port` to point the dev server at another dashboard.

## Structure

- `src/api/` — the snapshot types and REST commands. Types mirror `maasagentharness/dashboard/snapshot.py`.
- `src/state/` — the live store (revision-guarded snapshots, de-duplicated log events) and the stream hook.
- `src/components/` — the bench: readouts, the trace, worker rows, the budget editor, the scenario launcher, the log.
- `src/styles/` — design tokens (light and dark) and the stylesheet.
- `e2e/` — Playwright tests. They start the backend on a throwaway database.

## Design notes

The palette is validated with the dataviz skill's `validate_palette.js`. Each theme's series colours
pass lightness, chroma, colour-vision and contrast checks against that theme's background. Status is
never shown by colour alone.
