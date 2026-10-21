import { useEffect, useState, type ReactNode } from "react";
import { Home, Workflow, AudioLines, History, Plug, Phone, Database, BookOpen, Users, Sun, Moon, Monitor, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useApp } from "@/lib/app";
import { auth } from "@/lib/api";
import { cn, local } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Menu, MenuContent, MenuItem, MenuTrigger, Tip } from "@/components/ui/menu";

type NavItem = { id: string; label: string; icon: typeof Home; also?: string[]; admin?: boolean };
const NAV: { group: string; items: NavItem[] }[] = [
  { group: "Build", items: [
    { id: "overview", label: "Overview", icon: Home },
    { id: "agents", label: "Voice agents", icon: Workflow, also: ["build"] },
    { id: "test", label: "Test call", icon: AudioLines },
    { id: "calls", label: "Calls", icon: History },
  ] },
  { group: "Data", items: [
    { id: "data", label: "Data", icon: Database },
    { id: "knowledge", label: "Knowledge", icon: BookOpen },
  ] },
  { group: "Setup", items: [
    { id: "integrations", label: "Integrations", icon: Plug },
    { id: "phone", label: "Phone numbers", icon: Phone },
    { id: "team", label: "Team", icon: Users, admin: true },
  ] },
];

export function Mark({ className }: { className?: string }) {
  // Lampline: a switchboard lamp lit on its line (docs/brand/).
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <rect width="24" height="24" rx="6" fill="var(--mark-tile)" />
      <path d="M4 15.5H20" stroke="#f2b84b" strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="12" cy="15.5" r="3.2" fill="#f2b84b" />
      <path d="M8.2 9.6a5.4 5.4 0 0 1 7.6 0M6 7.2a8.6 8.6 0 0 1 12 0" fill="none" stroke="#f2b84b" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

type Theme = "light" | "dark" | "system";
function applyTheme(t: Theme) {
  local.set("vpe-theme", t === "system" ? null : t);
  const dark = t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
}
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(((local.get("vpe-theme") as Theme) || "system"));
  useEffect(() => {
    applyTheme(theme);
    if (theme !== "system") return;
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const on = () => applyTheme("system");
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [theme]);
  return [theme, setTheme] as const;
}

function ThemeMenu({ compact }: { compact: boolean }) {
  const [theme, setTheme] = useTheme();
  const Icon = theme === "light" ? Sun : theme === "dark" ? Moon : Monitor;
  return (
    <Menu>
      <MenuTrigger asChild>
        <Button variant="ghost" size={compact ? "icon-sm" : "sm"} className={cn(!compact && "justify-start")} aria-label="Theme">
          <Icon />{!compact && <span>{theme === "system" ? "Theme: match system" : theme === "dark" ? "Theme: dark" : "Theme: light"}</span>}
        </Button>
      </MenuTrigger>
      <MenuContent align="start" side="top">
        <MenuItem onSelect={() => setTheme("light")}><Sun />Light</MenuItem>
        <MenuItem onSelect={() => setTheme("dark")}><Moon />Dark</MenuItem>
        <MenuItem onSelect={() => setTheme("system")}><Monitor />Match system</MenuItem>
      </MenuContent>
    </Menu>
  );
}

export function Shell({ page, children, narrow }: { page: string; children: ReactNode; narrow?: boolean }) {
  const { health, me } = useApp();
  const [collapsed, setCollapsed] = useState(() => local.get("vpe-sidebar") === "collapsed");
  const compact = collapsed || !!narrow;
  const c = health?.config;
  const toggle = () => { local.set("vpe-sidebar", collapsed ? null : "collapsed"); setCollapsed(!collapsed); };
  return (
    <div className="flex h-full min-h-0 flex-col md:flex-row">
      <aside className={cn("flex shrink-0 flex-col border-b bg-sidebar md:h-full md:border-b-0 md:border-r transition-[width]",
        compact ? "md:w-[60px]" : "md:w-60")} aria-label="Main">
        <div className={cn("flex h-14 items-center gap-2.5 px-4", compact && "md:justify-center md:px-0")}>
          <Mark className="size-7 shrink-0" />
          <span className={cn("truncate text-[15px] font-semibold", compact && "md:hidden")}>{health?.brand || "Lampline"}</span>
          {health && <span className={cn("ml-auto font-mono text-[11px] text-muted-foreground", compact && "md:hidden")}>v{health.version}</span>}
        </div>
        <nav className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-1 md:flex-col md:overflow-visible md:pb-0">
          {NAV.map((g) => (
            <div key={g.group} className="contents md:block md:pt-3">
              <p className={cn("hidden px-2 pb-1.5 text-xs font-medium text-muted-foreground md:block", compact && "md:hidden")}>{g.group}</p>
              {g.items.filter((it) => !it.admin || me?.role === "admin").map((it) => {
                const active = page === it.id || (it.also || []).includes(page);
                const link = (
                  <a key={it.id} href={`#/${it.id === "overview" ? "" : it.id}`} aria-current={active ? "page" : undefined}
                    className={cn("flex h-9 shrink-0 items-center gap-2.5 rounded-md px-2.5 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground md:mb-0.5",
                      active && "bg-card text-foreground shadow-xs ring-1 ring-border", compact && "md:justify-center md:px-0")}>
                    <it.icon className={cn("size-[18px] shrink-0", active && "text-lamp")} />
                    <span className={cn("whitespace-nowrap", compact && "md:hidden")}>{it.label}</span>
                  </a>
                );
                return compact ? <Tip key={it.id} content={it.label} side="right">{link}</Tip> : link;
              })}
            </div>
          ))}
        </nav>
        <div className={cn("hidden gap-1 border-t p-2 md:grid", compact && "justify-items-center")}>
          <div className={cn("flex items-center gap-2 rounded-md px-2.5 py-2 text-xs text-muted-foreground", compact && "px-0")}>
            <Tip content={c ? (c.llm_ready ? `Engine running · ${c.model}` : "Engine running, but no model key") : "Connecting…"} side="right">
              <span className={cn("size-2 shrink-0 rounded-full", !c ? "bg-muted-foreground" : c.llm_ready ? "bg-ok shadow-[0_0_0_3px_color-mix(in_srgb,var(--ok)_22%,transparent)]" : "bg-warn")} />
            </Tip>
            {!compact && <span className="truncate">{c ? (c.llm_ready ? `Engine running · ${c.model}` : "Engine running · no model key") : "Connecting…"}</span>}
          </div>
          {me?.auth && !compact && (
            <p className="truncate px-2.5 pb-1 text-xs text-muted-foreground">Signed in as <span className="font-medium text-foreground">{me.name}</span> · {me.role}</p>
          )}
          <ThemeMenu compact={compact} />
          {!narrow && (
            <Button variant="ghost" size={compact ? "icon-sm" : "sm"} className={cn(!compact && "justify-start")} onClick={toggle}
              aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}>
              {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}{!compact && "Collapse"}
            </Button>
          )}
        </div>
      </aside>
      <main className="min-h-0 min-w-0 flex-1 overflow-auto" id="main">{children}</main>
    </div>
  );
}

export function Page({ title, description, actions, children, wide }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; children: ReactNode; wide?: boolean }) {
  return (
    <div className={cn("mx-auto w-full px-4 py-6 md:px-8 md:py-8", wide ? "max-w-[1400px]" : "max-w-6xl")}>
      <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div className="grid gap-1">
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          {description && <p className="max-w-[70ch] text-sm text-muted-foreground">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </header>
      {children}
    </div>
  );
}

export function KeyGate() {
  const { needKey, setNeedKey, refresh, health } = useApp();
  const [value, setValue] = useState("");
  useEffect(() => {                               // signed in by a key link while the page was open
    const on = () => { setNeedKey(false); location.reload(); };
    window.addEventListener("vpe-key", on);
    return () => window.removeEventListener("vpe-key", on);
  }, [setNeedKey]);
  return (
    <Dialog open={needKey} onOpenChange={() => {}}>
      <DialogContent onEscapeKeyDown={(e) => e.preventDefault()} onPointerDownOutside={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Enter the engine's API key</DialogTitle>
          <DialogDescription>
            {health?.container
              ? <>This engine runs in Docker, so it asks for its key once: <code>VPE_DOCKER_API_KEY</code> in <code>.env</code> (or open the link <code>scripts/docker-up.sh</code> printed).</>
              : <>This engine is reachable through a tunnel, so it asks for its key: <code>VPE_TUNNEL_API_KEY</code> in <code>.env</code>.</>}
          </DialogDescription>
        </DialogHeader>
        <form className="grid gap-3" onSubmit={async (e) => { e.preventDefault(); auth.set(value.trim()); setNeedKey(false); await refresh(); location.reload(); }}>
          <Input type="password" autoComplete="off" placeholder="vpe_…" value={value} onChange={(e) => setValue(e.target.value)} required autoFocus />
          <Button type="submit">Continue</Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function Notice({ children, tone = "info", className }: { children: ReactNode; tone?: "info" | "warn" | "bad"; className?: string }) {
  return (
    <div className={cn("flex gap-2.5 rounded-lg border px-3.5 py-3 text-[13px] leading-relaxed",
      tone === "info" && "bg-muted/60 text-muted-foreground", tone === "warn" && "border-warn/40 bg-warn/10", tone === "bad" && "border-bad/40 bg-bad/10 text-bad", className)}>
      {children}
    </div>
  );
}

export function Lamp({ on, warn, className }: { on?: boolean; warn?: boolean; className?: string }) {
  return <span className={cn("inline-block size-2 shrink-0 rounded-full", on ? "bg-ok" : warn ? "bg-warn" : "bg-bad", className)} />;
}
