import * as React from "react";
import { Tabs as T } from "radix-ui";
import { cn } from "@/lib/utils";

export const Tabs = T.Root;
export function TabsList({ className, ...props }: React.ComponentProps<typeof T.List>) {
  return <T.List className={cn("inline-flex h-9 items-center rounded-lg bg-muted p-1 text-muted-foreground", className)} {...props} />;
}
export function TabsTrigger({ className, ...props }: React.ComponentProps<typeof T.Trigger>) {
  return <T.Trigger className={cn("inline-flex h-7 flex-1 cursor-pointer items-center justify-center gap-1.5 rounded-md px-3 text-sm font-medium whitespace-nowrap transition-colors disabled:pointer-events-none disabled:opacity-50 data-[state=active]:bg-card data-[state=active]:text-foreground data-[state=active]:shadow-xs [&_svg]:size-4", className)} {...props} />;
}
export const TabsContent = T.Content;
