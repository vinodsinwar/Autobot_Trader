import { useEffect, useState } from "react";
import {
  HashRouter, Link, Navigate, Route, Routes, useLocation, useNavigate,
} from "react-router-dom";
import { api, hasToken, logout, setToken } from "./api";
import { useApi } from "./hooks";
import Channels from "./pages/Channels";
import Events from "./pages/Events";
import Overview from "./pages/Overview";
import Positions from "./pages/Positions";
import Reports from "./pages/Reports";
import Settings from "./pages/Settings";
import Signals from "./pages/Signals";
import Youtube from "./pages/Youtube";

const NAV = [
  ["/", "Overview"],
  ["/signals", "Signals"],
  ["/positions", "Positions & Orders"],
  ["/channels", "Telegram Channels"],
  ["/youtube", "YouTube"],
  ["/reports", "Reports"],
  ["/events", "Activity Log"],
  ["/settings", "Settings"],
] as const;

function Login() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const nav = useNavigate();
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const { token } = await api("/api/auth/login", { method: "POST", body: { password } });
      setToken(token);
      nav("/");
    } catch (err: any) {
      setError(err.message);
    }
  };
  return (
    <div className="min-h-screen grid place-items-center">
      <form onSubmit={submit} className="card w-80">
        <h1 className="text-lg font-semibold mb-1">Autobot Trader</h1>
        <p className="text-xs text-ink-dim mb-4">Sign in to the control panel</p>
        <input
          className="input" type="password" placeholder="Password" autoFocus
          value={password} onChange={(e) => setPassword(e.target.value)}
        />
        {error && <div className="text-loss text-sm mt-2">{error}</div>}
        <button className="btn-primary w-full mt-4">Sign in</button>
      </form>
    </div>
  );
}

function KillSwitch() {
  const { data, reload } = useApi<{ kill_switch: boolean; live_trading: boolean }>(
    "/api/overview", ["risk."],
  );
  if (!data) return null;
  const toggle = async () => {
    const msg = data.kill_switch
      ? "Release the kill switch and allow new orders?"
      : "ENGAGE the kill switch? All new signals will be blocked.";
    if (!confirm(msg)) return;
    await api("/api/kill-switch", { method: "POST", body: { on: !data.kill_switch } });
    reload();
  };
  return (
    <div className="flex flex-col gap-2">
      <button
        onClick={toggle}
        className={data.kill_switch ? "btn-danger w-full" : "btn w-full border-loss/50 text-loss"}
      >
        {data.kill_switch ? "⛔ KILL SWITCH ON" : "Kill switch"}
      </button>
      <div className={`text-xs text-center ${data.live_trading ? "text-warn" : "text-ink-mute"}`}>
        {data.live_trading ? "LIVE trading" : "Paper mode"}
      </div>
    </div>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  const loc = useLocation();
  const nav = useNavigate();
  useEffect(() => {
    if (!hasToken()) nav("/login");
  }, [loc.pathname]);
  return (
    <div className="flex min-h-screen">
      <aside className="w-52 shrink-0 border-r border-edge p-4 flex flex-col gap-1">
        <div className="font-semibold mb-4 px-2">
          ⚡ Autobot<span className="text-ink-mute">Trader</span>
        </div>
        {NAV.map(([path, label]) => (
          <Link
            key={path} to={path}
            className={`px-2 py-1.5 rounded text-sm ${
              loc.pathname === path ? "bg-panel text-ink" : "text-ink-dim hover:text-ink"
            }`}
          >
            {label}
          </Link>
        ))}
        <div className="mt-auto flex flex-col gap-3">
          <KillSwitch />
          <button className="text-xs text-ink-mute hover:text-ink text-left px-2"
                  onClick={() => { logout(); nav("/login"); }}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="flex-1 p-6 max-w-6xl">{children}</main>
    </div>
  );
}

export default function App() {
  return (
    <HashRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        {NAV.map(([path]) => (
          <Route
            key={path} path={path}
            element={
              hasToken() ? (
                <Shell>
                  {path === "/" && <Overview />}
                  {path === "/signals" && <Signals />}
                  {path === "/positions" && <Positions />}
                  {path === "/channels" && <Channels />}
                  {path === "/youtube" && <Youtube />}
                  {path === "/reports" && <Reports />}
                  {path === "/events" && <Events />}
                  {path === "/settings" && <Settings />}
                </Shell>
              ) : (
                <Navigate to="/login" />
              )
            }
          />
        ))}
        <Route path="*" element={<Navigate to="/" />} />
      </Routes>
    </HashRouter>
  );
}
