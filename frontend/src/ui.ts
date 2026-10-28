export const btn =
  "cursor-pointer rounded-lg border border-line-2 bg-raised px-2.5 py-1.5 text-fg hover:bg-hover disabled:cursor-default disabled:opacity-50";
export const btnPrimary =
  "cursor-pointer rounded-lg border border-neutral-200 bg-neutral-200 px-2.5 py-1.5 font-semibold text-neutral-900 hover:bg-white disabled:cursor-default disabled:opacity-50";
export const btnGhost =
  "cursor-pointer rounded-lg border border-line-2 bg-transparent px-2.5 py-1.5 text-fg hover:bg-raised disabled:cursor-default disabled:opacity-50";
export const card = "rounded-xl border border-line bg-panel p-4 shadow-[0_16px_40px_rgba(0,0,0,0.45)]";
export const input =
  "w-full rounded-lg border border-line bg-ink px-2.5 py-2 text-fg outline-none focus:border-neutral-500";
export const label = "text-xs text-muted";
export const muted = "text-muted";
export const mono = "font-mono text-xs text-muted";
export const empty =
  "rounded-xl border border-dashed border-line-2 px-6 py-9 text-center text-muted";
export const searchInput =
  "min-w-0 flex-1 rounded-lg border border-line bg-panel px-3 py-2 text-fg outline-none placeholder:text-faint focus:border-neutral-500";
export const sidePanel =
  "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-xl border border-line bg-panel lg:w-[420px] lg:flex-none xl:w-[480px]";
export const dangerBtn =
  "cursor-pointer rounded-lg border border-err/40 bg-transparent px-2.5 py-1.5 text-err hover:bg-err/10 disabled:cursor-default disabled:opacity-50";
export const btnDanger =
  "cursor-pointer rounded-lg border border-err bg-err px-2.5 py-1.5 font-semibold text-white hover:bg-[#ff6b64] disabled:cursor-default disabled:opacity-50";

export function chip(on: boolean) {
  return `shrink-0 cursor-pointer rounded-full border px-2.5 py-1 text-[11px] ${
    on ? "border-fg bg-fg text-ink" : "border-line bg-panel text-muted hover:text-fg"
  }`;
}

export function listRow(on: boolean) {
  return `mb-1 flex w-full cursor-pointer items-start gap-3 rounded-lg border px-3 py-2.5 text-left ${
    on ? "border-accent/70 bg-raised" : "border-transparent hover:bg-raised"
  }`;
}

const pillTones: Record<string, string> = {
  ok: "border-ok/40 text-ok",
  warn: "border-warn/40 text-warn",
  err: "border-err/40 text-err",
  accent: "border-accent/40 text-accent-2",
};

export function pill(tone = "") {
  return `inline-flex items-center gap-1.5 rounded-full border bg-raised px-2 py-0.5 text-[11px] ${pillTones[tone] ?? "border-line text-muted"}`;
}
