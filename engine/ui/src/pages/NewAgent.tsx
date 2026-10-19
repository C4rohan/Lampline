import { useEffect, useState } from "react";
import { FileInput, LayoutTemplate, Plus, Sparkles } from "lucide-react";
import { api } from "@/lib/api";
import { go } from "@/lib/route";
import { Button } from "@/components/ui/button";
import { Input, NativeSelect } from "@/components/ui/input";
import { Field } from "@/components/ui/label";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DescribeBox } from "./Describe";
import { ImportBox } from "./Import";

const TEMPLATE_LABEL: Record<string, string> = {
  "front-desk": "Front desk: root agent routing to appointments, hours, billing or a person",
  "appointment-scheduling": "Appointment scheduling: guided steps that collect and confirm details",
  "appointment-finder": "Appointment finder: looks up open times with an HTTP tool",
  "bilingual-front-desk": "Bilingual front desk: English and Spanish",
};

export function NewAgentButton({ variant = "default" }: { variant?: "default" | "outline" }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("");
  const [templates, setTemplates] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (open) api<{ templates: string[] }>("GET", "/v1/templates").then((r) => setTemplates(r.templates)).catch(() => {}); }, [open]);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button variant={variant}><Plus />New agent</Button></DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>New agent</DialogTitle>
          <DialogDescription>Describe it, start from a template, or bring one over from Dialogflow CX or CX Agent Studio.</DialogDescription>
        </DialogHeader>
        <Tabs defaultValue="describe" className="grid gap-4">
          <TabsList className="w-full">
            <TabsTrigger value="describe"><Sparkles />Describe it</TabsTrigger>
            <TabsTrigger value="template"><LayoutTemplate />Template or blank</TabsTrigger>
            <TabsTrigger value="import"><FileInput />Import</TabsTrigger>
          </TabsList>
          <TabsContent value="describe"><DescribeBox compact onDone={() => setOpen(false)} /></TabsContent>
          <TabsContent value="import"><ImportBox onDone={() => setOpen(false)} /></TabsContent>
          <TabsContent value="template">
        <form className="grid gap-4" onSubmit={async (e) => {
          e.preventDefault(); setBusy(true); setError("");
          try {
            const a = await api<{ slug: string }>("POST", "/v1/agents", { name, template });
            setOpen(false); go(`#/build/${encodeURIComponent(a.slug)}`);
          } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
        }}>
          <Field label="Name" htmlFor="na-name"><Input id="na-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="After-hours line" maxLength={80} required autoFocus /></Field>
          <Field label="Start from" htmlFor="na-template">
            <NativeSelect id="na-template" value={template} onChange={(e) => setTemplate(e.target.value)}>
              <option value="">Blank: Start, a root agent, one sub-agent and a goodbye</option>
              {templates.map((t) => <option key={t} value={t}>{TEMPLATE_LABEL[t] || t}</option>)}
            </NativeSelect>
          </Field>
          {error && <p className="text-sm text-bad">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" disabled={busy || !name.trim()}>{busy ? "Creating…" : "Create and open"}</Button>
          </DialogFooter>
        </form>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
