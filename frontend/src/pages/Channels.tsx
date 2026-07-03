import { useState } from "react";
import { api } from "../api";
import { Empty, ErrorNote, PageTitle } from "../components";
import { useApi } from "../hooks";

const emptyForm = {
  identifier: "", tg_chat_id: "", title: "", broker: "paper",
  execution_mode: "manual", rule_pack: "generic", llm_fallback: true,
  sizing_mode: "lots", sizing_value: 1,
};

export default function Channels() {
  const { data: channels, reload } = useApi<any[]>("/api/channels");
  const [form, setForm] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [testText, setTestText] = useState("");
  const [testResult, setTestResult] = useState<any>(null);

  const save = async () => {
    setError(null);
    try {
      const body: any = {
        title: form.title, broker: form.broker, execution_mode: form.execution_mode,
        rule_pack: form.rule_pack, llm_fallback: form.llm_fallback,
        sizing: { mode: form.sizing_mode, value: Number(form.sizing_value) },
      };
      if (form.id) {
        await api(`/api/channels/${form.id}`, { method: "PATCH", body });
      } else {
        if (form.tg_chat_id) body.tg_chat_id = Number(form.tg_chat_id);
        else body.identifier = form.identifier;
        await api("/api/channels", { method: "POST", body });
      }
      setForm(null);
      reload();
    } catch (e: any) {
      setError(e.message);
    }
  };

  const runTest = async () => {
    setTestResult(await api("/api/parse-test", {
      method: "POST",
      body: { text: testText, rule_pack: form?.rule_pack || "generic", use_llm: false },
    }));
  };

  return (
    <div>
      <PageTitle right={
        <button className="btn-primary" onClick={() => { setForm({ ...emptyForm }); setTestResult(null); }}>
          + Add channel
        </button>
      }>
        Telegram Channels
      </PageTitle>
      <ErrorNote error={error} />

      {form && (
        <div className="card mb-4">
          <div className="text-sm font-medium mb-1">{form.id ? `Edit: ${form.title}` : "Add channel"}</div>
          <div className="grid md:grid-cols-3 gap-x-4">
            {!form.id && (
              <div>
                <label className="label">@username / t.me link</label>
                <input className="input" value={form.identifier}
                       placeholder="@nifty_signals"
                       onChange={(e) => setForm({ ...form, identifier: e.target.value })} />
                <label className="label">…or numeric chat id</label>
                <input className="input" value={form.tg_chat_id} placeholder="-1001234567890"
                       onChange={(e) => setForm({ ...form, tg_chat_id: e.target.value })} />
              </div>
            )}
            <div>
              <label className="label">Title</label>
              <input className="input" value={form.title}
                     onChange={(e) => setForm({ ...form, title: e.target.value })} />
              <label className="label">Broker / market</label>
              <select className="input" value={form.broker}
                      onChange={(e) => setForm({ ...form, broker: e.target.value })}>
                <option value="paper">paper (simulated)</option>
                <option value="dhan">dhan (Indian F&O/equity)</option>
                <option value="delta">delta (crypto)</option>
              </select>
              <label className="label">Execution</label>
              <select className="input" value={form.execution_mode}
                      onChange={(e) => setForm({ ...form, execution_mode: e.target.value })}>
                <option value="manual">manual approval</option>
                <option value="auto">fully automatic</option>
              </select>
            </div>
            <div>
              <label className="label">Rule pack</label>
              <select className="input" value={form.rule_pack}
                      onChange={(e) => setForm({ ...form, rule_pack: e.target.value })}>
                <option value="generic">generic</option>
                <option value="index-options">index-options (BNF/NF aliases)</option>
                <option value="crypto">crypto</option>
              </select>
              <label className="label">Sizing</label>
              <div className="flex gap-2">
                <select className="input" value={form.sizing_mode}
                        onChange={(e) => setForm({ ...form, sizing_mode: e.target.value })}>
                  <option value="lots">lots</option>
                  <option value="units">units</option>
                  <option value="capital">capital ₹</option>
                </select>
                <input className="input" type="number" value={form.sizing_value}
                       onChange={(e) => setForm({ ...form, sizing_value: e.target.value })} />
              </div>
              <label className="label flex items-center gap-2 normal-case">
                <input type="checkbox" checked={form.llm_fallback}
                       onChange={(e) => setForm({ ...form, llm_fallback: e.target.checked })} />
                LLM fallback when rules fail
              </label>
            </div>
          </div>

          <div className="mt-4 border-t border-edge pt-3">
            <label className="label">Test bench — paste a real message from this channel</label>
            <div className="flex gap-2">
              <input className="input" value={testText}
                     placeholder="BUY NIFTY 25000 CE ABOVE 150 TGT 170/190 SL 130"
                     onChange={(e) => setTestText(e.target.value)} />
              <button className="btn" onClick={runTest}>Parse</button>
            </div>
            {testResult && (
              <div className={`text-sm mt-2 ${testResult.ok ? "text-ok" : "text-loss"}`}>
                {testResult.ok
                  ? `✓ ${testResult.summary} (${testResult.parser}, ${testResult.latency_ms}ms)`
                  : `✗ ${testResult.error}`}
              </div>
            )}
          </div>

          <div className="flex gap-2 mt-4">
            <button className="btn-primary" onClick={save}>Save</button>
            <button className="btn" onClick={() => setForm(null)}>Cancel</button>
          </div>
        </div>
      )}

      <div className="card">
        {channels?.length ? (
          <table className="data">
            <thead>
              <tr>
                <th>Title</th><th>Chat ID</th><th>Broker</th><th>Execution</th>
                <th>Rules</th><th>Enabled</th><th></th>
              </tr>
            </thead>
            <tbody>
              {channels.map((c) => (
                <tr key={c.id} className={c.enabled ? "" : "opacity-50"}>
                  <td className="font-medium">{c.title || "—"}</td>
                  <td className="text-xs text-ink-dim">{c.tg_chat_id}</td>
                  <td>{c.broker}</td>
                  <td>{c.execution_mode}</td>
                  <td className="text-xs">{c.rule_pack}{c.llm_fallback ? " + llm" : ""}</td>
                  <td>
                    <input type="checkbox" checked={c.enabled}
                           onChange={async (e) => {
                             await api(`/api/channels/${c.id}`, {
                               method: "PATCH", body: { enabled: e.target.checked },
                             });
                             reload();
                           }} />
                  </td>
                  <td>
                    <button className="btn text-xs" onClick={() => setForm({
                      id: c.id, title: c.title, broker: c.broker,
                      execution_mode: c.execution_mode, rule_pack: c.rule_pack,
                      llm_fallback: c.llm_fallback,
                      sizing_mode: c.sizing?.mode ?? "lots",
                      sizing_value: c.sizing?.value ?? 1,
                    })}>
                      Edit
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty text="No channels yet — add the Telegram channel you receive signals from" />
        )}
      </div>
    </div>
  );
}
