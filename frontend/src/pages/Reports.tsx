import { fmtMoney } from "../api";
import { Empty, PageTitle } from "../components";
import { useApi } from "../hooks";

export default function Reports() {
  const { data: sources } = useApi<any[]>("/api/reports/sources?days=90");
  const { data: daily } = useApi<any[]>("/api/reports/daily?days=90");

  const download = () => {
    const token = localStorage.getItem("ab_token");
    fetch("/api/reports/trades.csv?days=365", {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.text())
      .then((csv) => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
        a.download = "autobot-trades.csv";
        a.click();
      });
  };

  const monthly: Record<string, { pnl: number; trades: number; wins: number }> = {};
  for (const d of daily || []) {
    const m = d.date.slice(0, 7);
    monthly[m] = monthly[m] || { pnl: 0, trades: 0, wins: 0 };
    monthly[m].pnl += d.pnl;
    monthly[m].trades += d.trades;
    monthly[m].wins += d.wins;
  }

  return (
    <div>
      <PageTitle right={<button className="btn" onClick={download}>⬇ Export trades CSV</button>}>
        Reports
      </PageTitle>

      <div className="grid md:grid-cols-2 gap-4">
        <div className="card">
          <div className="text-sm font-medium mb-2">By signal source (90d)</div>
          {sources?.length ? (
            <table className="data">
              <thead>
                <tr><th>Source</th><th>Trades</th><th>Win rate</th><th>P&L</th></tr>
              </thead>
              <tbody>
                {sources.map((s) => (
                  <tr key={s.source}>
                    <td className="font-medium">{s.source}</td>
                    <td>{s.trades}</td>
                    <td>{s.win_rate != null ? `${(s.win_rate * 100).toFixed(0)}%` : "—"}</td>
                    <td className={s.pnl >= 0 ? "text-gain" : "text-loss"}>{fmtMoney(s.pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty text="No closed trades in the window" />
          )}
        </div>

        <div className="card">
          <div className="text-sm font-medium mb-2">Monthly P&L</div>
          {Object.keys(monthly).length ? (
            <table className="data">
              <thead>
                <tr><th>Month</th><th>Trades</th><th>Wins</th><th>P&L</th></tr>
              </thead>
              <tbody>
                {Object.entries(monthly).map(([m, v]) => (
                  <tr key={m}>
                    <td className="font-medium">{m}</td>
                    <td>{v.trades}</td>
                    <td>{v.wins}</td>
                    <td className={v.pnl >= 0 ? "text-gain" : "text-loss"}>{fmtMoney(v.pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty text="No data yet" />
          )}
        </div>
      </div>
    </div>
  );
}
