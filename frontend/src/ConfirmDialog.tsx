import { useEffect, useId, useRef, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { btn, btnDanger, btnPrimary } from "./ui";

export function ConfirmDialog({
  eyebrow,
  title,
  description,
  children,
  note,
  busy = false,
  confirmLabel = "confirmar",
  busyLabel = "confirmando…",
  cancelLabel = "voltar",
  tone = "primary",
  onCancel,
  onConfirm,
}: {
  eyebrow?: string;
  title: string;
  description: string;
  children?: ReactNode;
  note?: string;
  busy?: boolean;
  confirmLabel?: string;
  busyLabel?: string;
  cancelLabel?: string;
  tone?: "primary" | "danger";
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const copyId = useId();

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    (tone === "danger" ? cancelRef : confirmRef).current?.focus();
    return () => previous?.focus();
  }, [tone]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!busy) onCancel();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [busy, onCancel]);

  function onKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Tab" || !dialogRef.current) return;
    const items = [...dialogRef.current.querySelectorAll<HTMLElement>("button:not(:disabled)")];
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      onMouseDown={() => {
        if (!busy) onCancel();
      }}
    >
      <div className="absolute inset-0 bg-black/70 backdrop-blur-[3px]" />
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={copyId}
        tabIndex={-1}
        className="dialog-in relative w-full max-w-[440px] rounded-2xl border border-line bg-panel p-4 shadow-[0_28px_80px_rgba(0,0,0,0.55)] outline-none"
        onMouseDown={(event) => event.stopPropagation()}
        onKeyDown={onKeyDown}
      >
        {eyebrow ? <p className="m-0 text-[11px] font-medium tracking-wide text-faint uppercase">{eyebrow}</p> : null}
        <h2 id={titleId} className="m-0 mt-1 text-base font-semibold text-fg">
          {title}
        </h2>
        <p id={copyId} className="m-0 mt-1 text-xs leading-relaxed text-muted">
          {description}
        </p>
        {children}
        {note ? <p className="mt-3 rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-xs text-err">{note}</p> : null}
        <div className="mt-4 flex justify-end gap-2">
          <button ref={cancelRef} type="button" className={btn} disabled={busy} onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={tone === "danger" ? btnDanger : btnPrimary}
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? busyLabel : confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function ConfirmPair({
  leftSrc,
  leftLabel,
  leftKind = "frame",
  rightSrc,
  rightLabel,
}: {
  leftSrc?: string | null;
  leftLabel: string;
  leftKind?: "frame" | "photo";
  rightSrc?: string | null;
  rightLabel: string;
}) {
  return (
    <div className="mt-4 grid grid-cols-[1fr_auto_auto] items-center gap-3">
      <Media src={leftSrc} label={leftLabel} kind={leftKind} />
      <span className="pb-5 text-lg text-faint" aria-hidden="true">
        →
      </span>
      <Media src={rightSrc} label={rightLabel} kind="photo" />
    </div>
  );
}

function Media({ src, label, kind }: { src?: string | null; label: string; kind: "frame" | "photo" }) {
  const frame = kind === "frame";
  return (
    <figure className={`m-0 ${frame ? "min-w-0" : "w-24"}`}>
      {src ? (
        <img
          className={
            frame
              ? "aspect-video w-full rounded-lg border border-line bg-black object-contain"
              : "size-24 rounded-xl border border-line object-cover"
          }
          src={src}
          alt=""
        />
      ) : (
        <div
          className={
            frame
              ? "flex aspect-video items-center justify-center rounded-lg border border-dashed border-line-2 text-[11px] text-faint"
              : "size-24 rounded-xl border border-dashed border-line-2 bg-hover"
          }
        >
          {frame ? "sem frame" : null}
        </div>
      )}
      <figcaption className="mt-1.5 truncate text-center text-[11px] text-faint">{label}</figcaption>
    </figure>
  );
}
