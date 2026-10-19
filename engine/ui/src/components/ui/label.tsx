import * as React from "react";
import { cn } from "@/lib/utils";

export function Label({ className, ...props }: React.ComponentProps<"label">) {
  return <label data-slot="label" className={cn("text-sm font-medium leading-none select-none", className)} {...props} />;
}

/** Label, control and hint stacked, the way every form field here looks. */
export function Field({ label, hint, htmlFor, children, aside, className }: {
  label?: React.ReactNode; hint?: React.ReactNode; htmlFor?: string; children: React.ReactNode; aside?: React.ReactNode; className?: string;
}) {
  return (
    <div className={cn("grid gap-2", className)}>
      {(label || aside) && (
        <div className="flex min-h-5 items-center gap-2">
          {label && <Label htmlFor={htmlFor}>{label}</Label>}
          {aside && <span className="ml-auto">{aside}</span>}
        </div>
      )}
      {children}
      {hint && <p className="text-[12.5px] leading-snug text-muted-foreground">{hint}</p>}
    </div>
  );
}
