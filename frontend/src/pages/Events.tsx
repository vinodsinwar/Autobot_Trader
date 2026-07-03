import { useState } from "react";
import { fmtTime } from "../api";
import { Empty, PageTitle } from "../components";
import { useApi } from "../hooks";

const CATS = ["all", "signal", "order", "position", "risk", "telegram", "youtube", "system"];

export default function Events() {
  const [cat, setCat] = useState("all");
  const path = cat === "all" ? "/api/events?limit=200" : `/api/events?category=${cat}&limit=200`;
  const { data: events } = useApi<any[]>(path, ["signal.", "order.", "position.", "risk.", "telegram.", "youtube.", "system."]);

  return (
    <div>
      <PageTitle>Activity Log</PageTitle>
      <div className="flex gap-2 mb-4 flex-wrap">
        {CATS.map((c) => (
          <button key={c}
                  className={`btn text-xs ${cat === c ? "border-accent text-accent" : ""}`}
                  onClick={() => setCat(c)}>
            {c}
          </button>
        ))}
      </div>
      <div className="card">
        {events?.length ? (
          <table className="data">
            <thead>
              <tr><th>When</th><th>Category</th><th>Event</th><th>Details</th></tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td className="text-ink-dim whitespace-nowrap">{fmtTime(e.ts)}</td>
                  <td>{e.category}</td>
                  <td className="font-medium">{e.name.replace(/_/g, " ")}</td>
                  <td className="text-xs text-ink-dim break-all max-w-md">
                    {Object.entries(e.payload || {})
                      .filter(([k, v]) => v != null && v !== "" && k !== "signal_id")
                      .slice(0, 5)
                      .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`)
                      .join(" · ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty text="No events" />
        )}
      </div>
    </div>
  );
}
