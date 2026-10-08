# Frontend

Vue 3 + Vite UI for the migrated data. See the [project README](../../README.md) for setup.

```bash
nvm use && npm install
npm run dev     # http://localhost:5173, proxies /api to the backend on :8010
npm run build
```

Pages: `PatientList` (search by name), `PatientDetail` (identifiers + observations), `RunReport` (validation checklist).
