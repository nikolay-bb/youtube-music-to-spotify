"use client";

// Shared pieces. Deliberately plain: no component library, so someone reading
// this repo can follow the whole UI without learning a framework first.
//
// Every control answers a press the same way: it lifts a little on hover,
// shrinks a little while pressed, shows a spinner while it waits for the
// backend, and fades when it cannot be used. That consistency is what makes
// the app feel like it is listening.

import Link from "next/link";
import { useRef, useState } from "react";
import type { Verdict } from "@/lib/types";

export function Card({
  title,
  subtitle,
  children,
  accent,
  className = "",
}: {
  title?: string;
  subtitle?: string;
  children: React.ReactNode;
  accent?: "spotify" | "youtube";
  className?: string;
}) {
  const bar =
    accent === "spotify"
      ? "border-t-2 border-t-spotify"
      : accent === "youtube"
        ? "border-t-2 border-t-youtube"
        : "";
  return (
    <section
      className={`rounded-2xl border border-line bg-surface p-5 shadow-[0_10px_30px_rgb(0_0_0/0.25)] ${bar} ${className}`}
    >
      {title && (
        <header className="mb-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
        </header>
      )}
      {children}
    </section>
  );
}

type Variant = "primary" | "secondary" | "danger" | "spotify";
type Size = "sm" | "md";

const VARIANT_STYLES: Record<Variant, string> = {
  primary: "bg-ink text-canvas hover:bg-white",
  secondary:
    "border border-line bg-surface text-ink hover:border-muted/60 hover:bg-raised",
  danger: "border border-red-400/30 text-red-400 hover:bg-red-400/10",
  spotify: "bg-spotify text-black font-semibold hover:brightness-110",
};

// The lift-and-press motion. Buttons only get it while enabled; links have no
// :enabled state, so they get the plain form and fade via a class instead.
const MOTION_BUTTON =
  "enabled:hover:-translate-y-px enabled:active:translate-y-0 enabled:active:scale-[0.97]";
const MOTION_LINK = "hover:-translate-y-px active:translate-y-0 active:scale-[0.97]";

function buttonClasses(variant: Variant, size: Size, extra: string) {
  const sizing = size === "sm" ? "px-3 py-1.5 text-xs" : "px-4 py-2 text-sm";
  return `inline-flex select-none items-center justify-center gap-2 rounded-lg font-medium transition-[background-color,border-color,color,transform,opacity,filter,box-shadow] duration-150 ease-out disabled:cursor-not-allowed disabled:opacity-40 ${VARIANT_STYLES[variant]} ${sizing} ${extra}`;
}

export function Button({
  children,
  variant = "primary",
  size = "md",
  loading = false,
  disabled,
  type = "button",
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
  /** Waiting on the backend: shows a spinner and refuses a second press. */
  loading?: boolean;
}) {
  return (
    <button
      type={type}
      {...props}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={buttonClasses(variant, size, `${MOTION_BUTTON} ${className}`)}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
}

/** A link that looks and moves like a button.
 *
 *  Used instead of wrapping <Button> in <a>, which puts one interactive
 *  element inside another - invalid HTML that some browsers handle oddly.
 *  Paths inside the app use Next's Link; full URLs (the backend's CSV
 *  download, say) are plain links. */
export function ButtonLink({
  href,
  children,
  variant = "primary",
  size = "md",
  external = false,
  disabled = false,
  className = "",
  onClick,
}: {
  href: string;
  children: React.ReactNode;
  variant?: Variant;
  size?: Size;
  /** Open in a new tab. */
  external?: boolean;
  disabled?: boolean;
  className?: string;
  onClick?: React.MouseEventHandler<HTMLAnchorElement>;
}) {
  const classes = buttonClasses(
    variant,
    size,
    `${disabled ? "pointer-events-none opacity-40" : MOTION_LINK} ${className}`,
  );
  const shared = {
    className: classes,
    onClick,
    "aria-disabled": disabled || undefined,
    tabIndex: disabled ? -1 : undefined,
  };

  if (external || /^https?:/.test(href)) {
    return (
      <a
        href={href}
        target={external ? "_blank" : undefined}
        rel={external ? "noreferrer" : undefined}
        {...shared}
      >
        {children}
      </a>
    );
  }
  return (
    <Link href={href} {...shared}>
      {children}
    </Link>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent ${className}`}
      aria-hidden
    />
  );
}

/** A grey bar where content will be. Shown while a request is in flight, so
 *  the page has a shape before it has data. */
export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div aria-hidden className={`animate-pulse rounded-md bg-white/10 ${className}`} />
  );
}

/** A green tick or an empty ring. Used throughout the setup screen. */
export function StatusDot({ ok, pending }: { ok: boolean; pending?: boolean }) {
  if (pending) return <Spinner className="text-muted" />;
  return ok ? (
    <span
      className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-spotify text-[10px] font-bold text-black shadow-[0_0_10px_rgb(30_215_96/0.5)]"
      aria-label="ready"
    >
      ✓
    </span>
  ) : (
    <span
      className="inline-block h-4 w-4 shrink-0 rounded-full border-2 border-muted/50"
      aria-label="not ready"
    />
  );
}

/** A failure, in three parts: what happened, what to try, and a button to
 *  try it. The message is the sentence the backend sent; the hint is the
 *  next step; the action is a Try again or a Go to Connect. */
export function ErrorNote({
  title,
  hint,
  action,
  children,
}: {
  title?: string;
  hint?: string | null;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-red-400/25 bg-red-400/10 px-4 py-3 text-sm text-red-200"
    >
      {title && <p className="font-semibold">{title}</p>}
      <div className={`break-words ${title ? "mt-0.5" : ""}`}>{children}</div>
      {hint && <p className="mt-1.5 text-xs text-red-200/75">{hint}</p>}
      {action && <div className="mt-2.5 flex flex-wrap gap-2">{action}</div>}
    </div>
  );
}

export function InfoNote({
  tone = "info",
  children,
}: {
  tone?: "info" | "warn";
  children: React.ReactNode;
}) {
  const styles =
    tone === "warn"
      ? "border-amber-400/25 bg-amber-400/10 text-amber-200"
      : "border-sky-400/25 bg-sky-400/10 text-sky-200";
  return (
    <div className={`rounded-lg border px-4 py-3 text-sm ${styles}`}>{children}</div>
  );
}

/** Click-to-copy. Setup means pasting exact strings, and a typo costs an hour.
 *  The button says "Copied" for a moment so there is no doubt it worked; if
 *  the browser refuses clipboard access, the text is selected instead so a
 *  plain Cmd+C still gets it. */
export function CopyField({ value, label }: { value: string; label?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const text = useRef<HTMLElement>(null);

  function selectText() {
    if (!text.current) return;
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.selectAllChildren(text.current);
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setState("copied");
    } catch {
      selectText();
      setState("failed");
    }
    setTimeout(() => setState("idle"), 1600);
  }

  const buttonStyle =
    state === "copied"
      ? "border-spotify/60 bg-spotify/15 text-spotify"
      : state === "failed"
        ? "border-amber-400/40 bg-amber-400/10 text-amber-200"
        : "border-line bg-surface text-ink hover:bg-raised";

  return (
    <div className="mt-2">
      {label && <div className="mb-1 text-xs font-medium text-ink">{label}</div>}
      <div className="flex items-stretch gap-2">
        <code
          ref={text}
          onClick={selectText}
          className="flex-1 cursor-text overflow-x-auto rounded-md border border-line bg-black/40 px-2 py-1.5 font-mono text-xs whitespace-nowrap text-muted"
        >
          {value}
        </code>
        <button
          type="button"
          onClick={copy}
          aria-live="polite"
          className={`min-w-[4.5rem] shrink-0 rounded-md border px-2.5 text-xs transition-[background-color,border-color,color,transform] duration-150 active:scale-95 ${buttonStyle}`}
        >
          {state === "copied" ? "Copied ✓" : state === "failed" ? "Press ⌘C" : "Copy"}
        </button>
      </div>
    </div>
  );
}

const VERDICT_STYLES: Record<Verdict, { label: string; className: string }> = {
  matched: { label: "Matched", className: "bg-spotify/15 text-spotify" },
  low_confidence: { label: "Not sure", className: "bg-amber-400/15 text-amber-300" },
  not_found: { label: "Not found", className: "bg-white/10 text-muted" },
  error: { label: "Error", className: "bg-red-400/15 text-red-300" },
};

export function VerdictBadge({ verdict }: { verdict: Verdict }) {
  const { label, className } = VERDICT_STYLES[verdict];
  return (
    <span
      className={`inline-block whitespace-nowrap rounded px-2 py-0.5 text-xs font-medium ${className}`}
    >
      {label}
    </span>
  );
}

export function Stat({
  label,
  value,
  tone = "plain",
  hint,
}: {
  label: string;
  value: number | string;
  tone?: "plain" | "good" | "warn" | "bad";
  hint?: string;
}) {
  const toneClass = {
    plain: "text-ink",
    good: "text-spotify",
    warn: "text-amber-300",
    bad: "text-muted",
  }[tone];

  return (
    <div className="rounded-xl border border-line bg-surface px-4 py-3">
      <div className={`tabular text-2xl font-semibold ${toneClass}`}>{value}</div>
      <div className="mt-0.5 text-xs text-muted">{label}</div>
      {hint && <div className="mt-1 text-[11px] text-muted">{hint}</div>}
    </div>
  );
}

export function ProgressBar({
  done,
  total,
  animated = false,
}: {
  done: number;
  total: number;
  animated?: boolean;
}) {
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return (
    <div>
      <div className="relative h-2.5 w-full overflow-hidden rounded-full bg-white/10">
        <div
          className={`h-full rounded-full bg-gradient-to-r from-spotify-deep to-spotify shadow-[0_0_12px_rgb(30_215_96/0.45)] transition-[width] duration-500 ${
            animated ? "progress-active" : ""
          }`}
          style={{ width: `${percent}%` }}
        />
        {/* Nothing counted yet, so sweep instead of showing an empty bar. */}
        {animated && percent === 0 && (
          <span className="progress-sweep absolute top-0 h-full w-1/3 rounded-full bg-spotify/60" />
        )}
      </div>
      <p className="tabular mt-2 text-xs text-muted">
        {done.toLocaleString()} of {total.toLocaleString()} songs ({percent}%)
      </p>
    </div>
  );
}

/** One numbered step in the setup guide. */
export function Step({
  number,
  title,
  done,
  children,
}: {
  number: number;
  title: string;
  done?: boolean;
  children: React.ReactNode;
}) {
  return (
    <li className="flex gap-3">
      <span
        className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
          done ? "bg-spotify text-black" : "bg-raised text-muted"
        }`}
      >
        {done ? "✓" : number}
      </span>
      <div className="min-w-0 flex-1 pb-4">
        <h3 className="text-sm font-medium">{title}</h3>
        <div className="mt-1 text-xs leading-relaxed text-muted">{children}</div>
      </div>
    </li>
  );
}
