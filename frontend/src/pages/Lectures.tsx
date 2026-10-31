import { useEffect, useMemo, useState } from "react";
import { ConfirmDialog, ConfirmPair } from "../ConfirmDialog";
import { api, type Lecture, type Speaker } from "../api";
import { btn, btnPrimary, dangerBtn, empty, mono, muted, pill } from "../ui";

const tone: Record<string, string> = {
  done: "ok",
  pending: "",
  failed: "err",
  needs_review: "warn",
  skipped: "err",
  excluded: "",
  downloading: "accent",
  transcribing: "accent",
  summarizing: "accent",
  uploading: "accent",
  identifying: "accent",
};

const STATUS_LABEL: Record<string, string> = {
  done: "pronta",
  pending: "na fila",
  failed: "falhou",
  needs_review: "revisar",
  skipped: "ignorada",
  excluded: "excluída",
  downloading: "baixando",
  transcribing: "transcrevendo",
  summarizing: "resumindo",
  uploading: "enviando",
  identifying: "identificando",
};

const BUSY = new Set(["downloading", "identifying", "transcribing", "summarizing", "uploading"]);
const TOO_SMALL = "vídeo abaixo do tamanho mínimo (MIN_VIDEO_BYTES)";

const FILTERS: { id: string; label: string; match: (lecture: Lecture) => boolean }[] = [
  { id: "all", label: "Todas", match: () => true },
  {
    id: "attention",
    label: "Atenção",
    match: (lecture) => lecture.status === "failed" || lecture.status === "needs_review" || Boolean(lecture.warning),
  },
  { id: "pending", label: "Fila", match: (lecture) => lecture.status === "pending" },
  { id: "active", label: "Em curso", match: (lecture) => BUSY.has(lecture.status) },
  { id: "done", label: "Prontas", match: (lecture) => lecture.status === "done" },
  {
    id: "out",
    label: "Fora",
    match: (lecture) => lecture.status === "excluded" || lecture.status === "skipped",
  },
];

function statusLabel(status: string) {
  return STATUS_LABEL[status] || status;
}

function formatBytes(size?: number) {
  if (!size) return "—";
  if (size >= 1024 ** 3) return `${(size / 1024 ** 3).toFixed(2)} GiB`;
  if (size >= 1024 ** 2) return `${Math.round(size / 1024 ** 2)} MiB`;
  return `${Math.round(size / 1024)} KiB`;
}

function formatWhen(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "—";
  return date.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

function shortPath(path: string) {
  const parts = path.split("/").filter(Boolean);
  if (!parts.length) return "sem pasta";
  return parts.length <= 2 ? parts.join("/") : parts.slice(-2).join("/");
}

export function Lectures({
  lectures,
  speakers,
  onChange,
  focusId = null,
  focusToken = 0,
}: {
  lectures: Lecture[];
  speakers: Speaker[];
  onChange: () => Promise<void>;
  focusId?: number | null;
  focusToken?: number;
}) {
  const [selected, setSelected] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const current = lectures.find((lecture) => lecture.id === selected) ?? null;
  const activeFilter = FILTERS.find((item) => item.id === filter) ?? FILTERS[0];

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return lectures.filter((lecture) => {
      if (!activeFilter.match(lecture)) return false;
      if (!q) return true;
      return [lecture.video_filename, lecture.drive_path, lecture.speaker_name, lecture.lecture_date, lecture.error]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(q));
    });
  }, [lectures, query, activeFilter]);

  useEffect(() => {
    if (selected != null && !lectures.some((lecture) => lecture.id === selected)) setSelected(null);
  }, [lectures, selected]);

  useEffect(() => {
    if (focusId != null) setSelected(focusId);
  }, [focusId, focusToken]);

  useEffect(() => {
    if (selected == null) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelected(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

  if (!lectures.length) {
    return <div className={empty}>Nenhuma aula sincronizada ainda. Conecte o Drive e rode o pipeline.</div>;
  }

  return (
    <div className="flex min-h-0 flex-1 gap-3">
      <section className={`flex min-h-0 min-w-0 flex-1 flex-col ${current ? "max-lg:hidden" : ""}`}>
        <div className="mb-3 flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <input
              className="min-w-0 flex-1 rounded-lg border border-line bg-panel px-3 py-2 text-fg outline-none placeholder:text-faint focus:border-neutral-500"
              value={query}
              placeholder="Buscar vídeo, pasta, palestrante ou erro"
              onChange={(event) => setQuery(event.target.value)}
            />
            {query ? (
              <button className={btn} onClick={() => setQuery("")}>
                limpar
              </button>
            ) : null}
          </div>
          <div className="flex gap-1.5 overflow-x-auto pb-0.5">
            {FILTERS.map((item) => {
              const count = lectures.filter(item.match).length;
              const on = item.id === filter;
              return (
                <button
                  key={item.id}
                  className={`shrink-0 cursor-pointer rounded-full border px-2.5 py-1 text-[11px] ${
                    on ? "border-fg bg-fg text-ink" : "border-line bg-panel text-muted hover:text-fg"
                  }`}
                  onClick={() => setFilter(item.id)}
                >
                  {item.label}
                  <span className={on ? "text-ink/60" : "text-faint"}> {count}</span>
                </button>
              );
            })}
          </div>
          <p className="m-0 text-[11px] text-faint">
            {filtered.length} de {lectures.length} · clique na aula para manter
          </p>
        </div>
        <div className="min-h-0 flex-1 overflow-auto rounded-xl border border-line bg-panel">
          {filtered.length === 0 ? (
            <div className="px-4 py-10 text-center text-muted">Nenhuma aula neste filtro.</div>
          ) : (
            <ul className="m-0 list-none p-1.5">
              {filtered.map((lecture) => {
                const on = lecture.id === current?.id;
                return (
                  <li key={lecture.id}>
                    <button
                      type="button"
                      aria-current={on ? "true" : undefined}
                      className={`mb-1 flex w-full cursor-pointer items-start gap-3 rounded-lg border px-3 py-2.5 text-left ${
                        on ? "border-accent/70 bg-raised" : "border-transparent hover:bg-raised"
                      }`}
                      onClick={() => setSelected(lecture.id)}
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex items-baseline justify-between gap-3">
                          <span className="truncate font-medium text-fg">{lecture.video_filename}</span>
                          <span className={`${mono} shrink-0`}>{lecture.lecture_date || "sem data"}</span>
                        </div>
                        <div className="mt-0.5 truncate text-xs text-muted" title={lecture.drive_path}>
                          {shortPath(lecture.drive_path)} · {lecture.speaker_name || "sem palestrante"} · {formatBytes(lecture.video_size)}
                        </div>
                        {lecture.status === "failed" && lecture.error ? (
                          <p className="m-0 mt-1 truncate text-[11px] text-red-400" title={lecture.error}>
                            {lecture.error}
                          </p>
                        ) : lecture.warning ? (
                          <p className="m-0 mt-1 truncate text-[11px] text-warn" title={lecture.warning}>
                            {lecture.warning}
                          </p>
                        ) : null}
                      </div>
                      <span className={`${pill(tone[lecture.status] || "")} mt-0.5 shrink-0`}>
                        <span className="size-1.5 rounded-full bg-current" />
                        {statusLabel(lecture.status)}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </section>
      <LecturePanel lecture={current} speakers={speakers} onClose={() => setSelected(null)} onChange={onChange} />
    </div>
  );
}

function LecturePanel({
  lecture,
  speakers,
  onClose,
  onChange,
}: {
  lecture: Lecture | null;
  speakers: Speaker[];
  onClose: () => void;
  onChange: () => Promise<void>;
}) {
  const [transcript, setTranscript] = useState<string | null>(null);
  const [transcriptNote, setTranscriptNote] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [showTranscript, setShowTranscript] = useState(false);
  const [note, setNote] = useState("");
  const [newSpeaker, setNewSpeaker] = useState("");
  const [confirm, setConfirm] = useState<"speaker" | "exclude" | null>(null);
  const [busy, setBusy] = useState("");

  useEffect(() => {
    setTranscript(null);
    setTranscriptNote("");
    setExpanded(false);
    setShowTranscript(false);
    setNote("");
    setNewSpeaker("");
    setConfirm(null);
    setBusy("");
  }, [lecture?.id]);

  async function run(key: string, action: () => Promise<string>) {
    setBusy(key);
    setNote("");
    try {
      setNote(await action());
      await onChange();
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Falha na ação");
    } finally {
      setBusy("");
    }
  }

  async function loadTranscript(id: number) {
    setShowTranscript(true);
    if (transcript !== null) return;
    setTranscriptNote("");
    try {
      setTranscript(await api.transcript(id));
    } catch (err) {
      setTranscriptNote(err instanceof Error ? err.message : "Sem transcrição");
    }
  }

  if (!lecture) {
    return (
      <aside className="hidden min-h-0 w-[420px] shrink-0 flex-col justify-center rounded-xl border border-dashed border-line-2 px-6 text-center lg:flex xl:w-[480px]">
        <p className="m-0 text-sm text-fg">Selecione uma aula</p>
        <p className={`${muted} mt-1`}>O painel fica aberto enquanto você percorre a lista. Esc fecha.</p>
      </aside>
    );
  }

  const locked = BUSY.has(lecture.status);
  const canAssign = !locked;
  const canReprocess = lecture.status === "failed" || lecture.status === "needs_review";
  const canExclude = ["pending", "failed", "needs_review", "skipped"].includes(lecture.status);
  const canRestore = lecture.status === "excluded" && lecture.error !== TOO_SMALL;
  const pendingSpeaker = confirm === "speaker" ? (speakers.find((speaker) => speaker.id === Number(newSpeaker)) ?? null) : null;

  return (
    <>
    <aside className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-xl border border-line bg-panel lg:w-[420px] lg:flex-none xl:w-[480px]">
      <header className="flex items-start gap-3 border-b border-line px-4 py-3">
        <button className={`${btn} lg:hidden`} onClick={onClose}>
          voltar
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <h2 className="m-0 truncate text-sm font-semibold" title={lecture.video_filename}>
              {lecture.video_filename}
            </h2>
            <button className={`${btn} hidden lg:inline`} onClick={onClose} title="Esc">
              fechar
            </button>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <span className={pill(tone[lecture.status] || "")}>
              <span className="size-1.5 rounded-full bg-current" />
              {statusLabel(lecture.status)}
            </span>
            <span className={mono}>{lecture.lecture_date || "sem data"}</span>
            <span className={mono}>{formatBytes(lecture.video_size)}</span>
          </div>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
        <dl className="m-0 grid grid-cols-[88px_1fr] gap-x-3 gap-y-1.5 text-xs">
          <dt className="text-faint">Pasta</dt>
          <dd className="m-0 break-all text-fg" title={lecture.drive_path}>
            {lecture.drive_path || "—"}
          </dd>
          <dt className="text-faint">Palestrante</dt>
          <dd className="m-0 text-fg">
            {lecture.speaker_name || "não identificado"}
            {lecture.speaker_confidence != null ? (
              <span className="text-muted"> · {Math.round(lecture.speaker_confidence * 100)}%</span>
            ) : null}
          </dd>
          <dt className="text-faint">Identificação</dt>
          <dd className="m-0 text-fg">{lecture.identification_status || "—"}</dd>
          <dt className="text-faint">Atualizada</dt>
          <dd className="m-0 text-fg">{formatWhen(lecture.updated_at)}</dd>
        </dl>

        <div className="mt-3 flex flex-wrap gap-1.5">
          <Check on={lecture.transcribed} label="transcrita" />
          <Check on={lecture.summarized} label="resumo" />
          <Check on={lecture.uploaded} label="no Drive" />
          <Check on={Boolean(lecture.youtube_url)} label="no YouTube" />
        </div>

        {lecture.youtube_url ? (
          <p className="mt-3 text-xs">
            <span className="text-faint">YouTube: </span>
            <a
              href={lecture.youtube_url}
              target="_blank"
              rel="noreferrer"
              className="break-all text-accent-2 underline-offset-2 hover:underline"
              title={lecture.youtube_title || lecture.youtube_url}
            >
              {lecture.youtube_title || lecture.youtube_url}
            </a>
          </p>
        ) : null}

        {locked ? (
          <p className="mt-3 rounded-lg border border-accent/30 bg-accent/10 px-3 py-2 text-xs text-accent-2">
            O pipeline está nesta aula. A manutenção fica bloqueada até a etapa terminar.
          </p>
        ) : null}
        {lecture.warning ? (
          <p className="mt-3 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">{lecture.warning}</p>
        ) : null}
        {lecture.identification_note ? <p className={`${muted} mt-3 text-xs`}>{lecture.identification_note}</p> : null}
        {lecture.error ? (
          <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 p-2.5 text-xs text-red-300">
            <p className="m-0 whitespace-pre-wrap">{lecture.error}</p>
            <p className="m-0 mt-2 text-[11px] text-red-300/70">Detalhes em data/logs/erro-AAAA-MM-DD.log</p>
          </div>
        ) : null}
        {lecture.status === "excluded" && lecture.error === TOO_SMALL ? (
          <p className={`${muted} mt-3 text-xs`}>Ficou de fora pelo tamanho mínimo. Não volta para a fila.</p>
        ) : null}

        {lecture.frame_url ? (
          <img
            className="mt-3 max-h-52 w-full rounded-lg border border-line bg-black object-contain"
            src={lecture.frame_url}
            alt="frame da aula"
          />
        ) : null}

        <section className="mt-4 border-t border-line pt-3">
          <h3 className="m-0 mb-2 text-[11px] font-medium tracking-wide text-faint uppercase">Palestrante</h3>
          <div className="flex flex-col gap-2 sm:flex-row">
            <select
              className="min-w-0 flex-1 rounded-lg border border-line bg-ink px-2.5 py-2 text-xs text-fg disabled:opacity-50"
              value={newSpeaker}
              disabled={!canAssign || Boolean(busy)}
              onChange={(event) => setNewSpeaker(event.target.value)}
            >
              <option value="">Escolher outro…</option>
              {speakers
                .filter((speaker) => speaker.id !== lecture.speaker_id)
                .map((speaker) => (
                  <option key={speaker.id} value={speaker.id}>
                    {speaker.name}
                    {speaker.role ? ` · ${speaker.role}` : ""}
                  </option>
                ))}
            </select>
            <button
              className={btnPrimary}
              disabled={!canAssign || !newSpeaker || Boolean(busy)}
              onClick={() => {
                if (!speakers.some((speaker) => speaker.id === Number(newSpeaker))) return;
                setNote("");
                setConfirm("speaker");
              }}
            >
              {busy === "speaker" ? "aplicando…" : "aplicar"}
            </button>
          </div>
        </section>

        <section className="mt-4 border-t border-line pt-3">
          <h3 className="m-0 mb-2 text-[11px] font-medium tracking-wide text-faint uppercase">Arquivos</h3>
          <div className="flex flex-wrap gap-2">
            {lecture.drive_folder_id ? (
              <a
                className={btn}
                href={`https://drive.google.com/drive/folders/${lecture.drive_folder_id}`}
                target="_blank"
                rel="noreferrer"
              >
                pasta no Drive
              </a>
            ) : null}
            {lecture.summarized ? (
              <a className={btn} href={`/api/lectures/${lecture.id}/pdf`} target="_blank" rel="noreferrer">
                abrir PDF
              </a>
            ) : (
              <span className="px-1 py-1.5 text-xs text-faint">PDF ainda não existe</span>
            )}
            <button className={btn} disabled={!lecture.transcribed} onClick={() => void loadTranscript(lecture.id)}>
              {showTranscript ? "transcrição abaixo" : "ver transcrição"}
            </button>
            <button
              className={btn}
              disabled={locked || Boolean(busy)}
              onClick={() =>
                void run("download", async () => {
                  const res = await api.deleteDownload(lecture.id);
                  return res.removed ? "MP4 local removido." : "Não havia MP4 baixado.";
                })
              }
            >
              {busy === "download" ? "removendo…" : "remover download"}
            </button>
          </div>
        </section>

        <section className="mt-4 border-t border-line pt-3">
          <h3 className="m-0 mb-2 text-[11px] font-medium tracking-wide text-faint uppercase">Fila</h3>
          <div className="flex flex-wrap gap-2">
            {canReprocess ? (
              <button
                className={btnPrimary}
                disabled={Boolean(busy)}
                onClick={() =>
                  void run("retry", async () => {
                    await api.continueLecture(lecture.id);
                    return "Reprocessamento iniciado. Acompanhe no Painel.";
                  })
                }
              >
                {busy === "retry" ? "iniciando…" : "reprocessar"}
              </button>
            ) : null}
            {canReprocess ? (
              <select
                className={btn}
                disabled={Boolean(busy)}
                value=""
                onChange={(event) => {
                  const stage = event.target.value as "transcribe" | "summarize" | "upload";
                  if (!stage) return;
                  void run("retry", async () => {
                    await api.continueLecture(lecture.id, stage);
                    return "Reprocessamento iniciado. Acompanhe no Painel.";
                  });
                }}
              >
                <option value="">reprocessar a partir de…</option>
                <option value="transcribe">transcrição</option>
                <option value="summarize">resumo</option>
                <option value="upload">envio ao Drive</option>
              </select>
            ) : null}
            {canRestore ? (
              <button
                className={btnPrimary}
                disabled={Boolean(busy)}
                onClick={() =>
                  void run("restore", async () => {
                    await api.restoreLecture(lecture.id);
                    return "Aula voltou para a fila.";
                  })
                }
              >
                {busy === "restore" ? "restaurando…" : "voltar para a fila"}
              </button>
            ) : null}
            {canExclude ? (
              <button
                className={dangerBtn}
                disabled={Boolean(busy)}
                onClick={() => {
                  setNote("");
                  setConfirm("exclude");
                }}
              >
                {busy === "exclude" ? "excluindo…" : "excluir da importação"}
              </button>
            ) : null}
            {!canReprocess && !canRestore && !canExclude ? (
              <p className={`${muted} m-0 text-xs`}>Nenhuma ação de fila para este status.</p>
            ) : null}
          </div>
        </section>

        {note && !confirm ? <p className="mt-3 rounded-lg bg-raised px-3 py-2 text-xs text-fg">{note}</p> : null}

        {lecture.summary_text ? (
          <details className="mt-4 rounded-lg border border-line bg-ink">
            <summary className="cursor-pointer px-3 py-2 text-xs text-muted">Resumo</summary>
            <p className="m-0 border-t border-line px-3 py-2 text-xs whitespace-pre-wrap text-fg">{lecture.summary_text}</p>
          </details>
        ) : null}

        {showTranscript ? (
          <div className="mt-3 rounded-lg border border-line bg-ink">
            <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
              <span className="text-xs text-muted">Transcrição</span>
              <a className="text-xs text-fg underline" href={`/api/lectures/${lecture.id}/transcript`} target="_blank" rel="noreferrer">
                nova aba
              </a>
            </div>
            <div className="p-3">
              {transcriptNote ? <p className={muted}>{transcriptNote}</p> : null}
              {transcript === null && !transcriptNote ? <p className={muted}>Carregando…</p> : null}
              {transcript ? (
                <>
                  <button className={`${btn} mb-2`} onClick={() => setExpanded((value) => !value)}>
                    {expanded ? "recolher" : "expandir"}
                  </button>
                  <pre className={`${mono} m-0 overflow-auto whitespace-pre-wrap text-fg ${expanded ? "" : "max-h-64"}`}>
                    {transcript}
                  </pre>
                </>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </aside>
    {pendingSpeaker ? (
      <ConfirmDialog
        eyebrow="Refazer o PDF"
        title={pendingSpeaker.name}
        description={`${lecture.video_filename} passa para este palestrante. O PDF é refeito e substitui o arquivo no Drive.`}
        busy={busy === "speaker"}
        note={note}
        confirmLabel="trocar"
        busyLabel="aplicando…"
        onCancel={() => {
          if (!busy) setConfirm(null);
        }}
        onConfirm={() => {
          const speakerId = pendingSpeaker.id;
          void run("speaker", async () => {
            await api.assignSpeaker(lecture.id, speakerId);
            await api.continueLecture(lecture.id);
            setConfirm(null);
            setNewSpeaker("");
            return "Palestrante trocado. O PDF está sendo refeito; acompanhe no Painel.";
          });
        }}
      >
        <ConfirmPair
          leftSrc={lecture.frame_url}
          leftLabel="vídeo"
          rightSrc={pendingSpeaker.photo_url}
          rightLabel={pendingSpeaker.role || "cadastro"}
        />
      </ConfirmDialog>
    ) : null}
    {confirm === "exclude" ? (
      <ConfirmDialog
        tone="danger"
        eyebrow="Tirar da importação"
        title={lecture.video_filename}
        description="A aula sai da fila e o MP4 baixado é apagado. Depois dá para devolver à fila."
        busy={busy === "exclude"}
        note={note}
        confirmLabel="excluir"
        busyLabel="excluindo…"
        onCancel={() => {
          if (!busy) setConfirm(null);
        }}
        onConfirm={() => {
          void run("exclude", async () => {
            await api.excludeLecture(lecture.id);
            setConfirm(null);
            return "Aula excluída da importação.";
          });
        }}
      >
        {lecture.frame_url ? (
          <img className="mt-4 max-h-40 w-full rounded-lg border border-line bg-black object-contain" src={lecture.frame_url} alt="" />
        ) : null}
      </ConfirmDialog>
    ) : null}
    </>
  );
}

function Check({ on, label }: { on: boolean; label: string }) {
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[11px] ${on ? "border-ok/40 text-ok" : "border-line text-faint"}`}>
      {on ? "✓" : "·"} {label}
    </span>
  );
}
