import * as React from "react";
import { DropdownMenu as M, Tooltip as T } from "radix-ui";
import { cn } from "@/lib/utils";

export const Menu = M.Root;
export const MenuTrigger = M.Trigger;
export function MenuContent({ className, align = "end", ...props }: React.ComponentProps<typeof M.Content>) {
  return (
    <M.Portal>
      <M.Content align={align} sideOffset={6} className={cn("z-50 min-w-44 rounded-lg border bg-popover p-1 text-popover-foreground shadow-md data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95", className)} {...props} />
    </M.Portal>
  );
}
export function MenuItem({ className, ...props }: React.ComponentProps<typeof M.Item>) {
  return <M.Item className={cn("flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm outline-none select-none data-[highlighted]:bg-accent data-[disabled]:opacity-50 [&_svg]:size-4 [&_svg]:text-muted-foreground", className)} {...props} />;
}
export function MenuSeparator() { return <M.Separator className="-mx-1 my-1 h-px bg-border" />; }

export const TooltipProvider = T.Provider;
export function Tip({ content, children, side = "top" }: { content: React.ReactNode; children: React.ReactNode; side?: "top" | "bottom" | "left" | "right" }) {
  return (
    <T.Root delayDuration={300}>
      <T.Trigger asChild>{children}</T.Trigger>
      <T.Portal>
        <T.Content side={side} sideOffset={6} className="z-50 max-w-72 rounded-md bg-primary px-2.5 py-1.5 text-xs text-primary-foreground shadow-md animate-in fade-in-0">{content}</T.Content>
      </T.Portal>
    </T.Root>
  );
}
