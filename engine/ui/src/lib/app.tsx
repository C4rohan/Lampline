import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, auth, type Health } from "./api";

export type Me = { name: string; role: "admin" | "builder" | "viewer"; auth: boolean };
type AppState = { health: Health | null; me: Me | null; refresh: () => Promise<Health | null>; down: boolean; needKey: boolean; setNeedKey: (v: boolean) => void };
const Ctx = createContext<AppState>(null as unknown as AppState);

export function AppProvider({ children }: { children: ReactNode }) {
  const [health, setHealth] = useState<Health | null>(null);
  const [down, setDown] = useState(false);
  const [needKey, setNeedKey] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const refresh = useCallback(async () => {
    try {
      const h = await fetch("/v1/health", { headers: auth.key ? { "X-API-Key": auth.key } : {} }).then((r) => r.json()) as Health;
      setHealth(h); setDown(false);
      if (h.auth_required && !auth.key) setNeedKey(true);
      else api<Me>("GET", "/v1/me").then(setMe).catch(() => setMe(null));
      document.title = h.brand;
      return h;
    } catch { setDown(true); return null; }
  }, []);
  useEffect(() => { auth.onMissing(() => setNeedKey(true)); refresh(); }, [refresh]);
  return <Ctx.Provider value={{ health, me, refresh, down, needKey, setNeedKey }}>{children}</Ctx.Provider>;
}
export const useApp = () => useContext(Ctx);

/** Load something from the API; reload() re-fetches. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true;
    setError(null);
    fn().then((d) => { if (live) setData(d); }).catch((e) => { if (live) setError(e.message); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, n]);
  return { data, error, reload: () => setN((x) => x + 1), setData };
}

export { api };
