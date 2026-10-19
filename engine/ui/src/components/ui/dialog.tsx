import * as React from "react";
import { Dialog as D } from "radix-ui";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

export const Dialog = D.Root;
export const DialogTrigger = D.Trigger;
export const DialogClose = D.Close;

export function DialogContent({ className, children, ...props }: React.ComponentProps<typeof D.Content>) {
  return (
    <D.Portal>
      <D.Overlay className="fixed inset-0 z-50 bg-black/40 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0" />
      <D.Content className={cn("fixed left-1/2 top-1/2 z-50 grid w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 gap-4 rounded-xl border bg-card p-6 shadow-lg data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95", className)} {...props}>
        {children}
        <D.Close className="absolute right-4 top-4 rounded-sm opacity-70 hover:opacity-100 cursor-pointer" aria-label="Close"><X className="size-4" /></D.Close>
      </D.Content>
    </D.Portal>
  );
}
export function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("grid gap-1.5", className)} {...props} />;
}
export function DialogTitle({ className, ...props }: React.ComponentProps<typeof D.Title>) {
  return <D.Title className={cn("text-lg font-semibold leading-none", className)} {...props} />;
}
export function DialogDescription({ className, ...props }: React.ComponentProps<typeof D.Description>) {
  return <D.Description className={cn("text-sm text-muted-foreground", className)} {...props} />;
}
export function DialogFooter({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("flex flex-col-reverse gap-2 sm:flex-row sm:justify-end", className)} {...props} />;
}
