import { useEffect, useState } from "react";
import { api } from "../api";
import { ErrorNote, PageTitle } from "../components";
import { useApi } from "../hooks";

function SettingForm({ settingKey, title, hint, fields }: {
  settingKey: string;
  title: string;
  hint?: string;
  fields: { name: string; label: string; type?: "text" | "password" | "number" | "checkbox"; placeholder?: string }[];
}) {
  const [values, setValues] = useState<any>({});
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api(`/api/settings/${settingKey}`).then((d) => setValues(d.value || {}));
  }, [settingKey]);

  const save = async () => {
    setError(null);
    setStatus(null);
    try {
      await api(`/api/settings/${settingKey}`, { method: "PUT", body: values });
      setStatus("Saved ✓");
      setTimeout(() => setStatus(null), 2500);
    } catch (e: any) {
      setError(e.message);
    }
  };

  return (
    <div className="card">
      <div className="text-sm font-medium">{title}</div>
      {hint && <div className="text-xs text-ink-dim mt-1">{hint}</div>}
      {fields.map((f) => (
        <div key={f.name}>
          {f.type === "checkbox" ? (
            <label className="label flex items-center gap-2 normal-case">
              <input type="checkbox" checked={!!values[f.name]}
                     onChange={(e) => setValues({ ...values, [f.name]: e.target.checked })} />
              {f.label}
            </label>
          ) : (
            <>
              <label className="label">{f.label}</label>
              <input
                className="input"
                type={f.type === "password" ? "text" : (f.type || "text")}
                placeholder={f.placeholder}
                value={values[f.name] ?? ""}
                onChange={(e) => setValues({
                  ...values,
                  [f.name]: f.type === "number" ? Number(e.target.value) : e.target.value,
                })}
              />
            </>
          )}
        </div>
      ))}
      <div className="flex items-center gap-3 mt-4">
        <button className="btn-primary" onClick={save}>Save</button>
        {status && <span className="text-ok text-sm">{status}</span>}
      </div>
      <ErrorNote error={error} />
    </div>
  );
}

export default function Settings() {
  const { data: brokerStatus, reload } = useApi<any>("/api/brokers/status");
  const [syncMsg, setSyncMsg] = useState<string | null>(null);

  const sync = async (broker: string) => {
    setSyncMsg(`Syncing ${broker} instruments…`);
    try {
      const r = await api(`/api/brokers/${broker}/sync-instruments`, { method: "POST" });
      setSyncMsg(`${broker}: ${r.synced} instruments synced ✓`);
      reload();
    } catch (e: any) {
      setSyncMsg(`${broker} sync failed: ${e.message}`);
    }
  };

  const tokenHours = brokerStatus?.dhan?.token_expires_in_hours;

  return (
    <div>
      <PageTitle>Settings</PageTitle>

      {brokerStatus && (
        <div className="card mb-4 text-sm">
          <div className="text-sm font-medium mb-2">Connections</div>
          <div className="grid md:grid-cols-3 gap-3">
            <div>
              Telegram:{" "}
              <span className={brokerStatus.telegram_connected ? "text-ok" : "text-loss"}>
                {brokerStatus.telegram_connected ? "connected" : "not connected"}
              </span>
              {!brokerStatus.telegram_connected && (
                <div className="text-xs text-ink-dim mt-1">
                  Run <code>python -m app.ingestion.tg_login</code> on the server, then enable
                  AUTOBOT_ENABLE_TELEGRAM.
                </div>
              )}
            </div>
            <div>
              Dhan:{" "}
              {brokerStatus.dhan.configured ? (
                <span className={tokenHours != null && tokenHours < 2 ? "text-loss" : "text-ok"}>
                  token {tokenHours != null ? `expires in ~${tokenHours}h` : "set"}
                </span>
              ) : (
                <span className="text-ink-mute">not configured</span>
              )}
              <div className="text-xs text-ink-dim mt-1">
                Dhan tokens last 24h (SEBI rule) — paste a fresh one daily below.
              </div>
              <button className="btn text-xs mt-1" onClick={() => sync("dhan")}>
                Sync instruments {brokerStatus.dhan.instruments_synced ? "✓" : ""}
              </button>
            </div>
            <div>
              Delta:{" "}
              <span className={brokerStatus.delta.configured ? "text-ok" : "text-ink-mute"}>
                {brokerStatus.delta.configured
                  ? brokerStatus.delta.testnet ? "configured (TESTNET)" : "configured"
                  : "not configured"}
              </span>
              <div>
                <button className="btn text-xs mt-1" onClick={() => sync("delta")}>
                  Sync products {brokerStatus.delta.instruments_synced ? "✓" : ""}
                </button>
              </div>
            </div>
          </div>
          {syncMsg && <div className="text-xs text-ink-dim mt-2">{syncMsg}</div>}
        </div>
      )}

      <div className="grid md:grid-cols-2 gap-4">
        <SettingForm
          settingKey="risk" title="Risk limits"
          hint="Every signal passes these gates. live_trading OFF routes all orders to the paper broker."
          fields={[
            { name: "live_trading", label: "LIVE trading (off = paper mode)", type: "checkbox" },
            { name: "max_trades_per_day", label: "Max trades per day", type: "number" },
            { name: "max_concurrent_positions", label: "Max concurrent positions", type: "number" },
            { name: "max_capital_per_trade", label: "Max capital per trade", type: "number" },
            { name: "daily_loss_limit", label: "Daily loss limit (auto-halt)", type: "number" },
            { name: "dedup_window_minutes", label: "Duplicate window (minutes)", type: "number" },
          ]}
        />
        <SettingForm
          settingKey="llm" title="LLM parser (pluggable)"
          hint="Any litellm model string: anthropic/claude-haiku-4-5, openai/gpt-4o-mini, gemini/gemini-2.0-flash, ollama/llama3…"
          fields={[
            { name: "model", label: "Model", placeholder: "anthropic/claude-haiku-4-5" },
            { name: "api_key", label: "API key", type: "password" },
            { name: "api_base", label: "API base (Ollama/self-hosted, optional)" },
            { name: "confidence_threshold", label: "Min confidence (0-1)", type: "number" },
            { name: "fast_model", label: "Fast model for YouTube live path (optional)" },
            { name: "fast_api_key", label: "Fast model API key (optional)", type: "password" },
          ]}
        />
        <SettingForm
          settingKey="dhan" title="Dhan (Indian markets)"
          hint="Get the access token from web.dhan.co → DhanHQ Trading APIs. Valid 24h."
          fields={[
            { name: "client_id", label: "Client ID" },
            { name: "access_token", label: "Access token (24h)", type: "password" },
          ]}
        />
        <SettingForm
          settingKey="delta" title="Delta Exchange India (crypto)"
          hint="API keys from delta.exchange → API keys. Tick testnet to trade the India testnet first."
          fields={[
            { name: "api_key", label: "API key", type: "password" },
            { name: "api_secret", label: "API secret", type: "password" },
            { name: "testnet", label: "Use India testnet", type: "checkbox" },
          ]}
        />
        <SettingForm
          settingKey="stt" title="Speech-to-text (YouTube audio)"
          hint="Cloud streaming STT recommended for real-time. deepgram | openai | local (faster-whisper)."
          fields={[
            { name: "provider", label: "Provider", placeholder: "deepgram" },
            { name: "api_key", label: "API key", type: "password" },
            { name: "language", label: "Language hint", placeholder: "hi" },
          ]}
        />
        <SettingForm
          settingKey="notifications" title="Telegram notifications"
          hint="Order/position/risk alerts sent from your own account. Chat id: your Saved Messages or a private group."
          fields={[
            { name: "enabled", label: "Enabled", type: "checkbox" },
            { name: "chat_id", label: "Chat ID", type: "number" },
          ]}
        />
      </div>
    </div>
  );
}
