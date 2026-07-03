import {
  Bar, BarChart, CartesianGrid, Cell, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { fmtMoney, fmtTime } from "../api";
import { Badge, Empty, PageTitle, Pnl, StatTile } from "../components";
import { useApi } from "../hooks";

// chart palette (validated reference palette, dark-mode steps)
const C = { pos: "#3987e5", neg: "#e66767", grid: "#2a2e33", ink: "#a5a49b" };

export default function Overview() {
  const { data: ov } = useApi<any>("/api/overview", ["order.", "position.", "risk.", "signal."]);
  const { data: daily } = useApi<any[]>("/api/reports/daily?days=30", ["position."]);
  const { data: signals } = useApi<any[]>("/api/signals?limit=8", ["signal.", "order."]);

  return (
    <div>
      <PageTitle>Overview</PageTitle>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <StatTile label="P&L today" value={<Pnl value={ov?.realized_pnl_today} />} />
        <StatTile label="P&L total" value={<Pnl value={ov?.realized_pnl_total} />} />
        <StatTile label="Open positions" value={ov?.open_positions ?? "—"} />
        <StatTile
          label="Win rate"
          value={ov?.win_rate != null ? `${(ov.win_rate * 100).toFixed(0)}%` : "—"}
          sub={ov ? `${ov.closed_trades} closed trades` : undefined}
        />
      </div>

      <div className="grid md:grid-cols-2 gap-4 mb-6">
        <div className="card">
          <div className="text-sm font-medium mb-3">Daily P&L (30d)</div>
          {daily?.length ? (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={daily}>
                <CartesianGrid stroke={C.grid} vertical={false} />
                <XAxis dataKey="date" tick={{ fill: C.ink, fontSize: 11 }}
                       tickFormatter={(d) => d.slice(5)} stroke={C.grid} />
                <YAxis tick={{ fill: C.ink, fontSize: 11 }} stroke={C.grid} width={52}
                       tickFormatter={(v) => fmtMoney(v)} />
                <Tooltip
                  contentStyle={{ background: "#181b1e", border: "1px solid #2a2e33" }}
                  formatter={(v: number) => [fmtMoney(v), "P&L"]}
                />
                <Bar dataKey="pnl" radius={[4, 4, 0, 0]}>
                  {daily.map((d, i) => (
                    <Cell key={i} fill={d.pnl >= 0 ? C.pos : C.neg} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <Empty text="No closed trades yet" />
          )}
        </div>
        <div className="card">
          <div className="text-sm font-medium mb-3">Cumulative P&L</div>
          {daily?.length ? (
            <ResponsiveContainer width="100%" height={200}>
              <LineChart data={daily}>
                <CartesianGrid stroke={C.grid} vertical={false} />
                <XAxis dataKey="date" tick={{ fill: C.ink, fontSize: 11 }}
                       tickFormatter={(d) => d.slice(5)} stroke={C.grid} />
                <YAxis tick={{ fill: C.ink, fontSize: 11 }} stroke={C.grid} width={52}
                       tickFormatter={(v) => fmtMoney(v)} />
                <Tooltip
                  contentStyle={{ background: "#181b1e", border: "1px solid #2a2e33" }}
                  formatter={(v: number) => [fmtMoney(v), "Cumulative"]}
                />
                <Line type="monotone" dataKey="cumulative" stroke={C.pos}
                      strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <Empty text="No closed trades yet" />
          )}
        </div>
      </div>

      <div className="card">
        <div className="text-sm font-medium mb-2">Latest signals</div>
        {signals?.length ? (
          <table className="data">
            <thead>
              <tr><th>When</th><th>Source</th><th>Signal</th><th>State</th></tr>
            </thead>
            <tbody>
              {signals.map((s) => (
                <tr key={s.id}>
                  <td className="text-ink-dim whitespace-nowrap">{fmtTime(s.created_at)}</td>
                  <td>{s.source}</td>
                  <td className="font-medium">
                    {s.parsed?.symbol
                      ? `${s.parsed.action} ${s.parsed.symbol} ${s.parsed.strike ?? ""} ${s.parsed.option_type ?? ""}`
                      : <span className="text-ink-mute">{s.error?.slice(0, 60) || "—"}</span>}
                  </td>
                  <td><Badge state={s.state} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty text="Waiting for the first signal…" />
        )}
      </div>
    </div>
  );
}
