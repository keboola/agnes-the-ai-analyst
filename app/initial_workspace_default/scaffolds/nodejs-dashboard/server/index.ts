import express from "express";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { runQuery } from "./agnesQuery.js";

// `"type": "module"` (ESM) means Node needs the explicit `.js` extension above,
// even though the source is `agnesQuery.ts` — TS resolves the `.js` specifier to
// the `.ts` sibling at build time and keeps it verbatim in the emitted JS.
const __dirname = path.dirname(fileURLToPath(import.meta.url));
// The server runs from `server/dist/index.js`, so the built Vite SPA (`dist/` at
// the project root, per vite.config `outDir`) is two levels up: server/dist -> server -> root -> dist.
const distDir = path.resolve(__dirname, "..", "..", "dist");

const app = express();
const port = Number(process.env.PORT ?? 3000);

app.use(express.json());

// Required by the upstream data-app-python-js contract: the runtime health
// checker (and the Agnes wake-on-request proxy) probes `POST /` and expects
// a 2xx once the app is ready to serve traffic.
app.post("/", (_req, res) => {
  res.status(200).json({ status: "ok" });
});

app.get("/api/data", async (_req, res) => {
  try {
    const result = await runQuery("SELECT * FROM my_table LIMIT 100");
    res.json(result);
  } catch (err) {
    res.status(502).json({ error: String(err) });
  }
});

// Serve the built Vite SPA (npm run build -> dist/).
app.use(express.static(distDir));
app.get("*", (_req, res) => {
  res.sendFile(path.join(distDir, "index.html"));
});

app.listen(port, () => {
  // eslint-disable-next-line no-console
  console.log(`agnes-nodejs-dashboard listening on :${port}`);
});
