import { fmtMoney, fmtTime } from "../api";
import { Badge, Empty, PageTitle, Pnl } from "../components";
import { useApi } from "../hooks";

export default function Positions() {
  const { data: positions } = useApi<any[]>("/api/positions?limit=100", ["position.", "order."]);
  const { data: orders } = useApi<any[]>("/api/orders?limit=100", ["order."]);

  return (
    <div>
      <PageTitle>Positions &amp; Orders</PageTitle>
      <div className="card mb-4">
        <div className="text-sm font-medium mb-2">Positions</div>
        {positions?.length ? (
          <table className="data">
            <thead>
              <tr>
                <th>Opened</th><th>Broker</th><th>Symbol</th><th>Side</th>
                <th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.id}>
                  <td className="text-ink-dim whitespace-nowrap">{fmtTime(p.opened_at)}</td>
                  <td>{p.broker}</td>
                  <td className="font-medium">{p.symbol}</td>
                  <td>{p.side}</td>
                  <td className="tabular-nums">{Math.abs(p.qty)}</td>
                  <td className="tabular-nums">{fmtMoney(p.avg_entry_price)}</td>
                  <td className="tabular-nums">{fmtMoney(p.exit_price)}</td>
                  <td>{p.status === "closed" ? <Pnl value={p.realized_pnl} /> : "—"}</td>
                  <td><Badge state={p.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty text="No positions yet" />
        )}
      </div>

      <div className="card">
        <div className="text-sm font-medium mb-2">Orders</div>
        {orders?.length ? (
          <table className="data">
            <thead>
              <tr>
                <th>When</th><th>Broker</th><th>Order ID</th><th>Symbol</th>
                <th>Side</th><th>Qty</th><th>Type</th><th>Price</th><th>Fill</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => (
                <tr key={o.id}>
                  <td className="text-ink-dim whitespace-nowrap">{fmtTime(o.created_at)}</td>
                  <td>{o.broker}</td>
                  <td className="text-xs text-ink-dim">{o.broker_order_id}</td>
                  <td className="font-medium">{o.symbol}</td>
                  <td>{o.side}</td>
                  <td className="tabular-nums">{o.qty}</td>
                  <td className="text-xs">{o.order_type}</td>
                  <td className="tabular-nums">{fmtMoney(o.price)}</td>
                  <td className="tabular-nums">{fmtMoney(o.avg_fill_price)}</td>
                  <td><Badge state={o.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty text="No orders yet" />
        )}
      </div>
    </div>
  );
}
