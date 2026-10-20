import { useState } from "react";
import { Check, Copy, KeyRound, Plus, Trash2, Users } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { useApp, useLoad } from "@/lib/app";
import { Notice, Page } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/ui/label";

type Key = { id: string; name: string; role: Role; hint: string; created: number; last_used: number | null };
type Role = "admin" | "builder" | "viewer";

const ROLE: Record<Role, string> = {
  admin: "Everything: settings, integrations, keys, deleting calls.",
  builder: "Agents, data, knowledge, tests and triggers. Not settings or keys.",
  viewer: "Reads everything and runs test calls and chats. Changes nothing.",
};
const when = (s: number | null) => (s ? new Date(s * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "never");

export function TeamPage() {
  const { me } = useApp();
  const { data, reload, error } = useLoad(() => api<{ keys: Key[]; enabled: boolean }>("GET", "/v1/keys"));
  const [creating, setCreating] = useState(false);
  const [revoke, setRevoke] = useState<Key | null>(null);
  async function doRevoke() {
    if (!revoke) return;
    try { await api("DELETE", `/v1/keys/${revoke.id}`); toast.success(`${revoke.name}'s key no longer works`); setRevoke(null); reload(); }
    catch (x) { toast.error((x as Error).message); }
  }
  return (
    <Page title="Team"
      description="Give each person their own key with a role. Keys work in this app and the API; revoke one and it stops working at once."
      actions={data?.enabled ? <Button onClick={() => setCreating(true)}><Plus />New key</Button> : undefined}>
      {error && <Notice tone="bad" className="mb-5">{error}</Notice>}
      {data && !data.enabled && (
        <Notice className="mb-5"><KeyRound className="mt-0.5 size-4 shrink-0" />
          <span>Named keys need the owner key first. Start the engine with <code className="font-mono">VPE_API_KEY</code> set (the tunnel and Docker set one for you), then sign in with it here.</span>
        </Notice>
      )}
      {me?.auth && <p className="mb-4 text-sm text-muted-foreground">You're signed in as <span className="font-medium text-foreground">{me.name}</span> ({me.role}).</p>}
      {data?.enabled && (data.keys.length ? (
        <Card className="overflow-hidden p-0">
          <table className="w-full text-[13px]">
            <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
              <tr><th className="px-4 py-2 font-medium">Name</th><th className="px-4 py-2 font-medium">Role</th>
                <th className="hidden px-4 py-2 font-medium sm:table-cell">Key</th><th className="hidden px-4 py-2 font-medium md:table-cell">Last used</th><th className="w-12" /></tr>
            </thead>
            <tbody>
              {data.keys.map((k) => (
                <tr key={k.id} className="border-t">
                  <td className="px-4 py-2.5 font-medium">{k.name}</td>
                  <td className="px-4 py-2.5"><Badge variant={k.role === "admin" ? "lamp" : "outline"}>{k.role}</Badge></td>
                  <td className="hidden px-4 py-2.5 font-mono text-xs text-muted-foreground sm:table-cell">lpk_…{k.hint}</td>
                  <td className="hidden px-4 py-2.5 text-muted-foreground md:table-cell">{when(k.last_used)}</td>
                  <td className="px-2 py-1.5 text-right"><Button size="icon-sm" variant="ghost" aria-label={`Revoke ${k.name}'s key`} onClick={() => setRevoke(k)}><Trash2 /></Button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      ) : (
        <Card className="grid place-items-center gap-3 px-6 py-14 text-center">
          <Users className="size-8 text-muted-foreground" />
          <p className="font-medium">Only the owner key so far</p>
          <p className="max-w-md text-sm text-muted-foreground">Make a key for each person who builds or reviews agents, so you can see who's who and revoke one without changing everyone's.</p>
          <Button onClick={() => setCreating(true)}><Plus />New key</Button>
        </Card>
      ))}
      <div className="mt-6 grid gap-2 text-sm sm:grid-cols-3">
        {(Object.keys(ROLE) as Role[]).map((r) => (
          <div key={r} className="rounded-lg border px-3 py-2.5"><p className="font-medium capitalize">{r}</p><p className="text-muted-foreground">{ROLE[r]}</p></div>
        ))}
      </div>
      <p className="mt-4 text-xs text-muted-foreground">Settings changes still have to be made on this computer, whatever the role. Put the engine behind HTTPS before sharing keys over a network (docs/HTTPS.md).</p>
      <NewKey open={creating} onOpenChange={setCreating} onDone={reload} />
      <Dialog open={!!revoke} onOpenChange={(v) => !v && setRevoke(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Revoke {revoke?.name}'s key?</DialogTitle>
            <DialogDescription>It stops working right away, in this app and the API. You can make them a new one.</DialogDescription></DialogHeader>
          <DialogFooter><Button variant="outline" onClick={() => setRevoke(null)}>Keep it</Button><Button variant="destructive" onClick={doRevoke}><Trash2 />Revoke</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </Page>
  );
}

function NewKey({ open, onOpenChange, onDone }: { open: boolean; onOpenChange: (v: boolean) => void; onDone: () => void }) {
  const [name, setName] = useState(""), [role, setRole] = useState<Role>("builder"), [err, setErr] = useState("");
  const [made, setMade] = useState<string | null>(null), [copied, setCopied] = useState(false);
  function close(v: boolean) { if (!v) { setName(""); setRole("builder"); setErr(""); setMade(null); setCopied(false); } onOpenChange(v); }
  async function save(e: React.FormEvent) {
    e.preventDefault();
    try { const r = await api<{ key: string }>("POST", "/v1/keys", { name, role }); setMade(r.key); onDone(); }
    catch (x) { setErr((x as Error).message); }
  }
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent>
        {made ? (
          <div className="grid gap-4">
            <DialogHeader><DialogTitle>{name}'s key</DialogTitle>
              <DialogDescription>Copy it now and send it to them privately. It isn't shown again; only a fingerprint is kept.</DialogDescription></DialogHeader>
            <div className="flex gap-2">
              <Input readOnly value={made} className="font-mono text-xs" onFocus={(e) => e.currentTarget.select()} aria-label="The new key" />
              <Button variant="outline" onClick={async () => { await navigator.clipboard.writeText(made); setCopied(true); }}>{copied ? <Check /> : <Copy />}{copied ? "Copied" : "Copy"}</Button>
            </div>
            <DialogFooter><Button onClick={() => close(false)}>Done</Button></DialogFooter>
          </div>
        ) : (
          <form onSubmit={save} className="grid gap-4">
            <DialogHeader><DialogTitle>New key</DialogTitle><DialogDescription>One key per person.</DialogDescription></DialogHeader>
            <Field label="Name" htmlFor="nk-name"><Input id="nk-name" value={name} onChange={(e) => { setErr(""); setName(e.target.value); }} placeholder="e.g. Jordan" autoFocus /></Field>
            <fieldset className="grid gap-2">
              <legend className="mb-1 text-sm font-medium">Role</legend>
              {(Object.keys(ROLE) as Role[]).map((r) => (
                <label key={r} className="flex cursor-pointer items-start gap-2.5 rounded-lg border px-3 py-2 text-sm has-[:checked]:border-lamp has-[:checked]:bg-lamp-soft">
                  <input type="radio" name="role" value={r} checked={role === r} onChange={() => setRole(r)} className="mt-1 accent-[var(--lamp)]" />
                  <span><span className="font-medium capitalize">{r}</span><span className="block text-muted-foreground">{ROLE[r]}</span></span>
                </label>
              ))}
            </fieldset>
            {err && <p className="text-sm text-bad">{err}</p>}
            <DialogFooter><Button type="submit" disabled={!name.trim()}>Create key</Button></DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
