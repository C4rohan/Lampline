import * as React from "react";
import { cn } from "@/lib/utils";

export function Card({ className, ...props }: React.ComponentProps<"div">) {
  return <div data-slot="card" className={cn("rounded-xl border bg-card text-card-foreground shadow-xs", className)} {...props} />;
}
export function CardHeader({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("grid gap-1.5 px-6 pt-6", className)} {...props} />;
}
export function CardTitle({ className, ...props }: React.ComponentProps<"h3">) {
  return <h3 className={cn("text-base font-semibold leading-tight", className)} {...props} />;
}
export function CardDescription({ className, ...props }: React.ComponentProps<"p">) {
  return <p className={cn("text-sm text-muted-foreground", className)} {...props} />;
}
export function CardContent({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("px-6 py-5", className)} {...props} />;
}
export function CardFooter({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("flex flex-wrap items-center gap-3 border-t px-6 py-4", className)} {...props} />;
}
