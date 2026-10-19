import * as React from "react";
import { Switch as S, Checkbox as C } from "radix-ui";
import { cn } from "@/lib/utils";

export function Switch({ className, ...props }: React.ComponentProps<typeof S.Root>) {
  return (
    <S.Root data-slot="switch" className={cn("peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-transparent shadow-xs transition-colors outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-50 data-[state=checked]:bg-lamp data-[state=unchecked]:bg-input", className)} {...props}>
      <S.Thumb className="pointer-events-none block size-4 rounded-full bg-card shadow-sm ring-0 transition-transform data-[state=checked]:translate-x-[calc(100%-1px)] data-[state=unchecked]:translate-x-0.5" />
    </S.Root>
  );
}

export function Checkbox({ className, ...props }: React.ComponentProps<typeof C.Root>) {
  return (
    <C.Root data-slot="checkbox" className={cn("peer size-4 shrink-0 cursor-pointer rounded-[4px] border border-input bg-card shadow-xs outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:cursor-not-allowed disabled:opacity-50 data-[state=checked]:border-lamp data-[state=checked]:bg-lamp data-[state=checked]:text-[#1b1406]", className)} {...props}>
      <C.Indicator className="flex items-center justify-center">
        <svg viewBox="0 0 24 24" className="size-3.5" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
      </C.Indicator>
    </C.Root>
  );
}

/** A switch with its label and an optional one-line explanation. */
export function Toggle({ label, hint, checked, onCheckedChange, id, disabled }: {
  label: React.ReactNode; hint?: React.ReactNode; checked: boolean; onCheckedChange: (v: boolean) => void; id: string; disabled?: boolean;
}) {
  return (
    <div className="flex items-start gap-3">
      <Switch id={id} checked={checked} onCheckedChange={onCheckedChange} disabled={disabled} className="mt-0.5" />
      <label htmlFor={id} className="grid cursor-pointer gap-0.5 text-sm">
        <span className="font-medium leading-tight">{label}</span>
        {hint && <span className="text-[12.5px] leading-snug text-muted-foreground">{hint}</span>}
      </label>
    </div>
  );
}
