import * as React from "react";
import { cn } from "@/lib/utils";

const field = "w-full min-w-0 rounded-md border border-input bg-card px-3 text-sm shadow-xs transition-[color,box-shadow] outline-none placeholder:text-muted-foreground/80 focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/25 disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-bad aria-invalid:ring-bad/20";

export function Input({ className, type = "text", ...props }: React.ComponentProps<"input">) {
  return <input type={type} data-slot="input" className={cn(field, "h-9 py-1", className)} {...props} />;
}

export function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return <textarea data-slot="textarea" className={cn(field, "min-h-16 py-2 leading-relaxed resize-y", className)} {...props} />;
}

/** A styled native select: keyboard and screen-reader friendly, supports optgroups. */
export function NativeSelect({ className, children, ...props }: React.ComponentProps<"select">) {
  return (
    <div className={cn("relative", className)}>
      <select data-slot="select" className={cn(field, "h-9 appearance-none pr-9 cursor-pointer")} {...props}>{children}</select>
      <svg viewBox="0 0 24 24" aria-hidden="true" className="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6" /></svg>
    </div>
  );
}
