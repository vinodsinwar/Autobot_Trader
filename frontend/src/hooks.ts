import { useCallback, useEffect, useRef, useState } from "react";
import { api, BusEvent, subscribeEvents } from "./api";

/** Fetch JSON with manual + event-driven refresh. */
export function useApi<T = any>(path: string | null, refreshTopics: string[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const topicsRef = useRef(refreshTopics);
  topicsRef.current = refreshTopics;

  const reload = useCallback(() => {
    if (!path) return;
    api<T>(path)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(String(e.message)))
      .finally(() => setLoading(false));
  }, [path]);

  useEffect(() => {
    setLoading(true);
    reload();
  }, [reload]);

  useEffect(() => {
    if (!topicsRef.current.length) return;
    let timer: number | null = null;
    const unsub = subscribeEvents((e: BusEvent) => {
      if (topicsRef.current.some((t) => e.topic.startsWith(t))) {
        // debounce bursts of events into one reload
        if (timer) window.clearTimeout(timer);
        timer = window.setTimeout(reload, 300);
      }
    });
    return () => {
      unsub();
      if (timer) window.clearTimeout(timer);
    };
  }, [reload]);

  return { data, error, loading, reload };
}
