import { ReactNode } from "react";
import { fmtMoney } from "./api";

export function StatTile(props: {
  label: string;
  value: ReactNode;
  tone?: "gain" | "loss" | "warn" | "neutral";
  sub?: string;
}) {
  const tone =
    props.tone === "gain" ? "text-gain" :
    props.tone === "loss" ? "text-loss" :
    props.tone === "warn" ? "text-warn" : "text-ink";
  return (
    <div className="card">
      <div className="text-xs uppercase tracking-wide text-ink-mute">{props.label}</div>
      <div className={`text-2xl font-semibold mt-1 tabular-nums ${tone}`}>{props.value}</div>
      {props.sub && <div className="text-xs text-ink-dim mt-1">{props.sub}</div>}
    </div>
  );
}

export function Pnl({ value }: { value: number | null | undefined }) {
  if (value == null) return <span className="text-ink-mute">—</span>;
  const tone = value > 0 ? "text-gain" : value < 0 ? "text-loss" : "text-ink-dim";
  return <span className={`tabular-nums ${tone}`}>{value > 0 ? "+" : ""}{fmtMoney(value)}</span>;
}

const STATE_TONE: Record<string, string> = {
  open: "bg-gain/20 text-gain",
  executing: "bg-accent/20 text-accent",
  closed: "bg-edge text-ink-dim",
  parsed: "bg-edge text-ink-dim",
  awaiting_approval: "bg-warn/20 text-warn",
  parse_failed: "bg-loss/20 text-loss",
  risk_rejected: "bg-loss/20 text-loss",
  approval_rejected: "bg-edge text-ink-mute",
  error: "bg-loss/20 text-loss",
  cancelled: "bg-edge text-ink-mute",
  filled: "bg-gain/20 text-gain",
  pending: "bg-warn/20 text-warn",
  placed: "bg-accent/20 text-accent",
  live: "bg-loss/20 text-loss",
  capturing: "bg-gain/20 text-gain",
  review: "bg-warn/20 text-warn",
  done: "bg-edge text-ink-dim",
};

export function Badge({ state }: { state: string }) {
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium ${STATE_TONE[state] ?? "bg-edge text-ink-dim"}`}>
      {state.replace(/_/g, " ")}
    </span>
  );
}

export function Empty({ text }: { text: string }) {
  return <div className="text-ink-mute text-sm py-8 text-center">{text}</div>;
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="text-loss text-sm py-2">{error}</div>;
}

export function PageTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-4">
      <h1 className="text-lg font-semibold">{children}</h1>
      {right}
    </div>
  );
}
