import { useState } from "react";
import { api, fmtTime } from "../api";
import { Badge, Empty, ErrorNote, PageTitle } from "../components";
import { useApi } from "../hooks";

const FILTERS = ["all", "awaiting_approval", "open", "executing", "closed",
                 "risk_rejected", "parse_failed", "error"] as const;

export default function Signals() {
  const [filter, setFilter] = useState<string>("all");
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const path = filter === "all" ? "/api/signals?limit=100" : `/api/signals?state=${filter}&limit=100`;
  const { data: signals, reload } = useApi<any[]>(path, ["signal.", "order.", "risk."]);

  const act = async (id: number, action: "approve" | "reject") => {
    setBusy(id);
    setError(null);
    try {
      await api(`/api/signals/${id}/${action}`, { method: "POST" });
      reload();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      <PageTitle>Signals</PageTitle>
      <div className="flex gap-2 mb-4 flex-wrap">
        {FILTERS.map((f) => (
          <button key={f}
                  className={`btn text-xs ${filter === f ? "border-accent text-accent" : ""}`}
                  onClick={() => setFilter(f)}>
            {f.replace(/_/g, " ")}
          </button>
        ))}
      </div>
      <ErrorNote error={error} />
      <div className="card">
        {signals?.length ? (
          <table className="data">
            <thead>
              <tr>
                <th>When</th><th>Source</th><th>Parsed signal</th>
                <th>Parser</th><th>State</th><th></th>
              </tr>
            </thead>
            <tbody>
              {signals.map((s) => (
                <tr key={s.id}>
                  <td className="text-ink-dim whitespace-nowrap">{fmtTime(s.created_at)}</td>
                  <td>{s.source}</td>
                  <td>
                    {s.parsed?.symbol ? (
                      <div>
                        <span className="font-medium">
                          {s.parsed.action} {s.parsed.symbol} {s.parsed.strike ?? ""}{" "}
                          {s.parsed.option_type ?? ""}
                        </span>
                        <div className="text-xs text-ink-dim">
                          entry {s.parsed.entry_type?.toLowerCase()} {s.parsed.entry_price ?? "mkt"}
                          {s.parsed.targets?.length ? ` · tgt ${s.parsed.targets.join("/")}` : ""}
                          {s.parsed.stop_loss != null ? ` · sl ${s.parsed.stop_loss}` : ""}
                        </div>
                      </div>
                    ) : (
                      <span className="text-ink-mute text-xs">{s.error?.slice(0, 80)}</span>
                    )}
                  </td>
                  <td className="text-xs text-ink-dim">{s.parser}</td>
                  <td><Badge state={s.state} /></td>
                  <td className="whitespace-nowrap">
                    {s.state === "awaiting_approval" && (
                      <div className="flex gap-1">
                        <button className="btn-primary text-xs" disabled={busy === s.id}
                                onClick={() => act(s.id, "approve")}>
                          Execute
                        </button>
                        <button className="btn text-xs" disabled={busy === s.id}
                                onClick={() => act(s.id, "reject")}>
                          Dismiss
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty text="No signals match this filter" />
        )}
      </div>
    </div>
  );
}
