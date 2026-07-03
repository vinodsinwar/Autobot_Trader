/** Minimal typed API client + live event stream. */

let token = localStorage.getItem("ab_token") || "";

export function setToken(t: string) {
  token = t;
  localStorage.setItem("ab_token", t);
}
export function hasToken() {
  return !!token;
}
export function logout() {
  token = "";
  localStorage.removeItem("ab_token");
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T = any>(
  path: string,
  opts: { method?: string; body?: any } = {},
): Promise<T> {
  const resp = await fetch(path, {
    method: opts.method || "GET",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (resp.status === 401 && !path.includes("/auth/login")) {
    logout();
    window.location.href = "/#/login";
    throw new ApiError(401, "session expired");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new ApiError(resp.status, String(detail));
  }
  const ct = resp.headers.get("content-type") || "";
  return ct.includes("json") ? resp.json() : ((await resp.text()) as any);
}

export type BusEvent = { topic: string; payload: Record<string, any>; ts: string };

/** Subscribe to the server event stream; auto-reconnects. Returns unsubscribe. */
export function subscribeEvents(onEvent: (e: BusEvent) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let retry = 1000;

  const connect = () => {
    if (closed || !token) return;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${window.location.host}/api/ws?token=${token}`);
    ws.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data));
      } catch {
        /* ignore malformed */
      }
    };
    ws.onopen = () => (retry = 1000);
    ws.onclose = () => {
      if (!closed) {
        setTimeout(connect, retry);
        retry = Math.min(retry * 2, 15000);
      }
    };
  };
  connect();
  return () => {
    closed = true;
    ws?.close();
  };
}

export const fmtMoney = (v: number | null | undefined) =>
  v == null ? "—" : v.toLocaleString("en-IN", { maximumFractionDigits: 2 });

export const fmtTime = (iso: string) =>
  new Date(iso).toLocaleString("en-IN", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
