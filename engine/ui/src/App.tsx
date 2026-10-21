import { lazy, Suspense } from "react";
import { Toaster } from "sonner";
import { AppProvider, useApp } from "@/lib/app";
import { useRoute } from "@/lib/route";
import { KeyGate, Page, Shell } from "@/components/shell";
import { TooltipProvider } from "@/components/ui/menu";
import { Overview } from "@/pages/Overview";
import { Agents } from "@/pages/Agents";
import { TestCall } from "@/pages/TestCall";
import { Integrations } from "@/pages/Integrations";
import { PhoneNumbers } from "@/pages/Phone";
import { DataPage } from "@/pages/Data";
import { KnowledgePage } from "@/pages/Knowledge";
import { CallsPage } from "@/pages/Calls";
import { TeamPage } from "@/pages/Team";
import { EvalsPage } from "@/pages/Evals";

const Builder = lazy(() => import("@/builder/Builder"));

function Routes() {
  const route = useRoute();
  const { down } = useApp();
  if (down) {
    return (
      <Shell page={route.page}>
        <Page title="Engine not reachable" description="Start it with scripts/engine-up.sh, then reload this page."><span /></Page>
      </Shell>
    );
  }
  const page = route.page;
  return (
    <Shell page={page} narrow={page === "build"}>
      {page === "build" ? (
        <Suspense fallback={<div className="p-8 text-sm text-muted-foreground">Opening the builder…</div>}>
          <Builder key={route.arg} slug={route.arg} />
        </Suspense>
      ) : page === "agents" ? <Agents />
        : page === "test" ? <TestCall key={route.params.get("agent") || ""} params={route.params} />
        : page === "integrations" ? <Integrations />
        : page === "phone" ? <PhoneNumbers />
        : page === "data" ? <DataPage />
        : page === "knowledge" ? <KnowledgePage />
        : page === "calls" ? <CallsPage />
        : page === "evals" ? <EvalsPage />
        : page === "team" ? <TeamPage />
        : <Overview />}
    </Shell>
  );
}

export function App() {
  return (
    <AppProvider>
      <TooltipProvider>
        <Routes />
        <KeyGate />
        <Toaster position="bottom-right" toastOptions={{ className: "!font-sans" }} />
      </TooltipProvider>
    </AppProvider>
  );
}
