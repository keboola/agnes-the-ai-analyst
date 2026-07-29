import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

interface DataResponse {
  columns: string[];
  rows: unknown[][];
  row_count: number;
}

/**
 * Dashboard shell. Replace this with your real layout — this scaffold
 * exists so an agent has a working, real (not placeholder) app to iterate
 * on from the first commit: it already renders data fetched through the
 * Express /api/data route (backed by server/agnesQuery.ts), no charting
 * library loaded from a CDN.
 */
export default function App() {
  const [data, setData] = useState<DataResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Relative (no leading slash) on purpose: Agnes serves the app under
    // `/apps/<slug>/` in path-prefix mode, so a root-anchored `/api/data` would
    // hit the Agnes host, not this app. `new URL(..., document.baseURI)` anchors
    // it to wherever the page was served from — `/apps/<slug>/api/data` in prod
    // (the ingress proxy strips the prefix back to `/api/data`), and `/api/data`
    // under `vite dev` (where the page is at the root and the dev proxy forwards).
    fetch(new URL("api/data", document.baseURI))
      .then((res) => {
        if (!res.ok) throw new Error(`request failed: ${res.status}`);
        return res.json();
      })
      .then(setData)
      .catch((err) => setError(String(err)));
  }, []);

  const chartData =
    data?.rows.map((row, i) => {
      const point: Record<string, unknown> = { index: i };
      data.columns.forEach((col, j) => {
        point[col] = row[j];
      });
      return point;
    }) ?? [];

  return (
    <div className="min-h-screen bg-slate-50 p-8">
      <h1 className="text-2xl font-semibold text-slate-900">Dashboard</h1>
      <p className="mt-1 text-sm text-slate-500">
        Data served through the Agnes API — edit src/App.tsx and
        server/index.ts to build your real dashboard.
      </p>

      {error && (
        <div className="mt-6 rounded-md bg-red-50 p-4 text-sm text-red-700">
          {error}
        </div>
      )}

      {!error && data && (
        <div className="mt-6 h-80 rounded-lg bg-white p-4 shadow">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="index" />
              <YAxis />
              <Tooltip />
              {data.columns.slice(1).map((col) => (
                <Bar key={col} dataKey={col} fill="#4f46e5" />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
