import { useState } from "react";
import { api, fmtTime } from "../api";
import { Badge, Empty, ErrorNote, PageTitle } from "../components";
import { useApi } from "../hooks";

export default function Youtube() {
  const { data: channels, reload } = useApi<any[]>("/api/youtube/channels", ["youtube."]);
  const [url, setUrl] = useState("");
  const [vodUrl, setVodUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const { data: streams, reload: reloadStreams } = useApi<any[]>(
    selected ? `/api/youtube/streams?channel_id=${selected}` : null, ["youtube."],
  );
  const [streamDetail, setStreamDetail] = useState<any>(null);

  const addChannel = async () => {
    setError(null);
    try {
      await api("/api/youtube/channels", { method: "POST", body: { url } });
      setUrl("");
      reload();
    } catch (e: any) {
      setError(e.message);
    }
  };

  const queueVod = async () => {
    if (!selected) return;
    setError(null);
    try {
      await api("/api/youtube/vod", { method: "POST", body: { yt_channel_id: selected, url: vodUrl } });
      setVodUrl("");
      reloadStreams();
    } catch (e: any) {
      setError(e.message);
    }
  };

  const openStream = async (id: number) => {
    setStreamDetail(await api(`/api/youtube/streams/${id}`));
  };

  const label = async (streamId: number, extractedId: number, verdict: "correct" | "wrong") => {
    await api("/api/youtube/feedback", {
      method: "POST",
      body: { stream_id: streamId, yt_extracted_signal_id: extractedId, label: verdict },
    });
    openStream(streamId);
  };

  const addMissed = async (streamId: number) => {
    const excerpt = prompt("Paste the transcript excerpt where the missed signal was spoken:");
    if (!excerpt) return;
    await api("/api/youtube/feedback", {
      method: "POST", body: { stream_id: streamId, label: "missed", excerpt },
    });
    openStream(streamId);
  };

  const rebuild = async (channelId: number) => {
    const r = await api(`/api/youtube/channels/${channelId}/rebuild-profile`, { method: "POST" });
    alert(`Profile rebuilt: ${r.examples} examples, ${r.lexicon_patterns} lexicon patterns` +
          (r.precision != null ? `, precision ${(r.precision * 100).toFixed(0)}%` : ""));
    reload();
  };

  const sel = channels?.find((c) => c.id === selected);

  return (
    <div>
      <PageTitle>YouTube Sources</PageTitle>
      <ErrorNote error={error} />

      <div className="card mb-4">
        <label className="label">Subscribe to a trader's channel (URL or @handle)</label>
        <div className="flex gap-2">
          <input className="input" value={url} placeholder="https://youtube.com/@sometrader"
                 onChange={(e) => setUrl(e.target.value)} />
          <button className="btn-primary whitespace-nowrap" onClick={addChannel}>+ Subscribe</button>
        </div>
        <div className="text-xs text-ink-dim mt-2">
          When this channel goes live, audio is transcribed in real time, detected signals are posted
          to your Telegram signal group and (after your approval) executed. Train it on past videos below.
        </div>
      </div>

      <div className="card mb-4">
        {channels?.length ? (
          <table className="data">
            <thead>
              <tr>
                <th>Channel</th><th>Status</th><th>Execution</th><th>Broker</th>
                <th>Calibration</th><th></th>
              </tr>
            </thead>
            <tbody>
              {channels.map((c) => (
                <tr key={c.id} className={c.enabled ? "" : "opacity-50"}>
                  <td className="font-medium">{c.title || c.handle || c.url}</td>
                  <td>{c.live ? <Badge state="live" /> : <span className="text-ink-mute text-xs">offline</span>}</td>
                  <td>
                    <select className="input text-xs py-0.5" value={c.execution_mode}
                            onChange={async (e) => {
                              await api(`/api/youtube/channels/${c.id}`, {
                                method: "PATCH", body: { execution_mode: e.target.value },
                              });
                              reload();
                            }}>
                      <option value="manual">manual</option>
                      <option value="auto">auto</option>
                    </select>
                  </td>
                  <td>
                    <select className="input text-xs py-0.5" value={c.broker}
                            onChange={async (e) => {
                              await api(`/api/youtube/channels/${c.id}`, {
                                method: "PATCH", body: { broker: e.target.value },
                              });
                              reload();
                            }}>
                      <option value="paper">paper</option>
                      <option value="dhan">dhan</option>
                      <option value="delta">delta</option>
                    </select>
                  </td>
                  <td className="text-xs text-ink-dim">
                    {c.calibration?.examples
                      ? `${c.calibration.examples} labels` +
                        (c.calibration.precision != null
                          ? ` · precision ${(c.calibration.precision * 100).toFixed(0)}%`
                          : "")
                      : "untrained"}
                  </td>
                  <td className="whitespace-nowrap">
                    <button className="btn text-xs mr-1" onClick={() => setSelected(c.id)}>
                      Streams
                    </button>
                    <button className="btn text-xs" onClick={() => rebuild(c.id)}>
                      Rebuild profile
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty text="No YouTube channels yet" />
        )}
      </div>

      {sel && (
        <div className="card mb-4">
          <div className="text-sm font-medium mb-2">
            Calibration studio — {sel.title || sel.handle}
          </div>
          <label className="label">Process a past stream (VOD URL) for training</label>
          <div className="flex gap-2">
            <input className="input" value={vodUrl}
                   placeholder="https://youtube.com/watch?v=…"
                   onChange={(e) => setVodUrl(e.target.value)} />
            <button className="btn-primary whitespace-nowrap" onClick={queueVod}>Process VOD</button>
          </div>

          {streams?.length ? (
            <table className="data mt-3">
              <thead>
                <tr><th>Video</th><th>Kind</th><th>Status</th><th>Started</th><th></th></tr>
              </thead>
              <tbody>
                {streams.map((s) => (
                  <tr key={s.id}>
                    <td className="font-medium">{s.title || s.video_id}</td>
                    <td>{s.kind}</td>
                    <td><Badge state={s.status} /></td>
                    <td className="text-ink-dim text-xs">{s.started_at ? fmtTime(s.started_at) : "—"}</td>
                    <td>
                      <button className="btn text-xs" onClick={() => openStream(s.id)}>
                        Review
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty text="No streams processed yet" />
          )}
        </div>
      )}

      {streamDetail && (
        <div className="card">
          <div className="flex items-center justify-between mb-2">
            <div className="text-sm font-medium">
              Review: {streamDetail.title || streamDetail.video_id}
            </div>
            <div className="flex gap-2">
              <button className="btn text-xs" onClick={() => addMissed(streamDetail.id)}>
                + Add missed signal
              </button>
              <button className="btn text-xs" onClick={() => setStreamDetail(null)}>Close</button>
            </div>
          </div>

          {streamDetail.extracted?.length ? (
            <table className="data">
              <thead>
                <tr><th>t</th><th>Heard (excerpt)</th><th>Extracted</th><th>Conf</th><th>Verdict</th></tr>
              </thead>
              <tbody>
                {streamDetail.extracted.map((x: any) => (
                  <tr key={x.id}>
                    <td className="text-ink-dim text-xs whitespace-nowrap">
                      {Math.floor(x.t_in_stream / 60)}:{String(Math.floor(x.t_in_stream % 60)).padStart(2, "0")}
                    </td>
                    <td className="text-xs text-ink-dim max-w-xs">{x.transcript_excerpt}</td>
                    <td className="font-medium text-sm">
                      {x.parsed?.action} {x.parsed?.symbol} {x.parsed?.strike ?? ""} {x.parsed?.option_type ?? ""}
                      {x.parsed?.entry_price ? ` @${x.parsed.entry_price}` : ""}
                      {x.parsed?.stop_loss != null ? ` SL ${x.parsed.stop_loss}` : ""}
                    </td>
                    <td className="tabular-nums text-xs">{(x.confidence * 100).toFixed(0)}%</td>
                    <td className="whitespace-nowrap">
                      {x.feedback ? (
                        <span className={`text-xs ${x.feedback === "correct" ? "text-ok" : "text-loss"}`}>
                          {x.feedback}
                        </span>
                      ) : (
                        <>
                          <button className="btn text-xs mr-1"
                                  onClick={() => label(streamDetail.id, x.id, "correct")}>✓</button>
                          <button className="btn text-xs"
                                  onClick={() => label(streamDetail.id, x.id, "wrong")}>✗</button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty text={
              streamDetail.status === "done" || streamDetail.status === "review"
                ? "No signals were detected in this video"
                : `Processing… (${streamDetail.status})`
            } />
          )}

          {streamDetail.segments?.length > 0 && (
            <details className="mt-3">
              <summary className="text-xs text-ink-dim cursor-pointer">
                Transcript ({streamDetail.segments.length} segments)
              </summary>
              <div className="text-xs text-ink-dim mt-2 max-h-64 overflow-y-auto space-y-1">
                {streamDetail.segments.map((seg: any) => (
                  <div key={seg.id}>
                    <span className="text-ink-mute tabular-nums">
                      {Math.floor(seg.t_start / 60)}:{String(Math.floor(seg.t_start % 60)).padStart(2, "0")}
                    </span>{" "}
                    {seg.text}
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>
      )}
    </div>
  );
}
