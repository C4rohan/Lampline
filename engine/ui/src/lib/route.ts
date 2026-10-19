import { useEffect, useState } from "react";

export type Route = { page: string; arg: string; params: URLSearchParams };

function parse(): Route {
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  const [page, ...rest] = path.split("/");
  return { page: page || "overview", arg: decodeURIComponent(rest.join("/")), params: new URLSearchParams(query || "") };
}

/** Hash routes: #/agents, #/build/<agent>, #/test?agent=<agent> ... */
export function useRoute(): Route {
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export function go(hash: string) { location.hash = hash; }
