import { api } from "../api";
import type { Lecture, Pipeline, Settings } from "../api";
import { card, mono, muted } from "../ui";

const STAGES = ["sync", "download", "identify", "transcribe", "summarize", "upload", "done"];

export function Dashboard({
  settings,
  pipeline,
  lectures,
  review,
  onOpenLecture,
  onOpenReview,
}: {
  settings: Settings | null;
  pipeline: Pipeline | null;
  lectures: Lecture[];
  review: Lecture[];
  onOpenLecture?: (id: number) => void;
  onOpenReview?: (id: number) => void;
}) {
  const done = lectures.filter((l) => l.status === "done").length;
  const pending = lectures.filter((l) => l.status === "pending" || l.status === "failed").length;
  const failed = lectures.filter((l) => l.status === "failed").length;
  const free = pipeline?.work_free_bytes;
  const freeLabel =
    free == null ? "—" : free >= 1024 ** 3 ? `${(free / 1024 ** 3).toFixed(1)} GB` : `${Math.round(free / 1024 ** 2)} MB`;

  return (
    <>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Metric label="Processadas" value={done} />
        <Metric label="Pendentes" value={pending} />
        <Metric label="Revisar palestrante" value={review.length} />
      </div>
      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div className={card}>
          <h3 className="m-0 mb-1.5 text-xs font-medium text-muted">Vídeos no Drive</h3>
          <div className="text-[28px] font-semibold tracking-tight">{pipeline?.drive_video_count ?? "—"}</div>
          <p className={muted}>último sync</p>
        </div>
        <Metric label="Livre em work/" value={freeLabel} />
        <Metric label="Falhas" value={failed} />
      </div>

      {review.length || lectures.some((lecture) => lecture.status === "failed") ? (
        <Attention lectures={lectures} review={review} onOpenLecture={onOpenLecture} onOpenReview={onOpenReview} />
      ) : null}

      <div className="mt-6 mb-3 flex items-baseline justify-between">
        <h2 className="m-0 text-[15px] font-semibold">Pipeline</h2>
        <span className={muted}>{pipeline?.message || "ocioso"}</span>
      </div>
      <div className={card}>
        <div className="flex flex-wrap gap-2">
          {STAGES.map((stage) => (
            <div
              key={stage}
              className={`rounded-lg px-2 py-1.5 text-[11px] ${
                pipeline?.stage === stage ? "bg-[#2a211c] text-fg" : "bg-raised text-faint"
              }`}
            >
              {stage}
            </div>
          ))}
        </div>
        <p className={`${muted} mt-3`}>
          Um vídeo por vez · Whisper {settings?.whisper_model} · IA {settings?.ai_provider} ({settings?.ai_model})
        </p>
        {pipeline?.current_filename ? <p className={mono}>{pipeline.current_filename}</p> : null}
        {(pipeline?.stage === "download" || pipeline?.stage === "transcribe") && pipeline.progress != null ? (
          <div className="mt-3">
            <div className="h-1.5 w-full overflow-hidden rounded bg-raised">
              <div className="h-full bg-fg transition-all" style={{ width: `${pipeline.progress}%` }} />
            </div>
            <p className={`${muted} mt-1`}>{pipeline.stage === "download" ? "Baixando" : "Transcrevendo"}… {pipeline.progress}%</p>
          </div>
        ) : null}
        {pipeline?.prefetch_filename ? (
          <div className="mt-3">
            <div className="h-1.5 w-full overflow-hidden rounded bg-raised">
              <div className="h-full bg-muted transition-all" style={{ width: `${pipeline.prefetch_progress ?? 0}%` }} />
            </div>
            <p className={`${muted} mt-1`}>
              Baixando o próximo em paralelo: {pipeline.prefetch_filename} · {pipeline.prefetch_progress ?? 0}%
            </p>
          </div>
        ) : null}
        {pipeline?.running ? (
          <button className="mt-3 rounded-lg bg-raised px-3 py-1.5 text-xs text-fg" onClick={() => void api.cancel()}>
            Cancelar
          </button>
        ) : null}
      </div>
    </>
  );
}

function Attention({
  lectures,
  review,
  onOpenLecture,
  onOpenReview,
}: {
  lectures: Lecture[];
  review: Lecture[];
  onOpenLecture?: (id: number) => void;
  onOpenReview?: (id: number) => void;
}) {
  const failed = lectures.filter((lecture) => lecture.status === "failed").slice(0, 6);
  const pendingReview = review.slice(0, 6);
  return (
    <div className="mt-6">
      <h2 className="m-0 mb-3 text-[15px] font-semibold">Precisa de atenção</h2>
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div className={card}>
          <h3 className="m-0 mb-2 text-xs font-medium text-muted">Falhas</h3>
          {failed.length === 0 ? (
            <p className={muted}>Nenhuma falha.</p>
          ) : (
            failed.map((lecture) => (
              <button
                key={lecture.id}
                className="mb-1 block w-full cursor-pointer rounded-lg border border-transparent px-2 py-1.5 text-left hover:bg-raised"
                onClick={() => onOpenLecture?.(lecture.id)}
              >
                <span className="block truncate text-fg">{lecture.video_filename}</span>
                <span className="block truncate text-[11px] text-red-400">{lecture.error || "falhou"}</span>
              </button>
            ))
          )}
        </div>
        <div className={card}>
          <h3 className="m-0 mb-2 text-xs font-medium text-muted">Revisar palestrante</h3>
          {pendingReview.length === 0 ? (
            <p className={muted}>Fila vazia.</p>
          ) : (
            pendingReview.map((lecture) => (
              <button
                key={lecture.id}
                className="mb-1 block w-full cursor-pointer rounded-lg border border-transparent px-2 py-1.5 text-left hover:bg-raised"
                onClick={() => onOpenReview?.(lecture.id)}
              >
                <span className="block truncate text-fg">{lecture.video_filename}</span>
                <span className={`${muted} block truncate text-[11px]`}>{lecture.speaker_name || "sem sugestão"}</span>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className={card}>
      <h3 className="m-0 mb-1.5 text-xs font-medium text-muted">{label}</h3>
      <div className="text-[28px] font-semibold tracking-tight">{value}</div>
    </div>
  );
}
