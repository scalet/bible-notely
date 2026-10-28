import { useEffect, useMemo, useState } from "react";
import { ConfirmDialog, ConfirmPair } from "../ConfirmDialog";
import { api, type Lecture, type Speaker } from "../api";
import { btn, btnPrimary, empty, listRow, mono, muted, searchInput, sidePanel } from "../ui";

export function Review({
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
  const [picked, setPicked] = useState<number | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const current = lectures.find((lecture) => lecture.id === selected) ?? null;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return lectures.filter((lecture) => {
      if (!q) return true;
      return [lecture.video_filename, lecture.drive_path, lecture.speaker_name, lecture.lecture_date, lecture.identification_note]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(q));
    });
  }, [lectures, query]);

  const orderedSpeakers = useMemo(() => {
    if (!current?.speaker_id) return speakers;
    return [...speakers].sort((a, b) => Number(b.id === current.speaker_id) - Number(a.id === current.speaker_id));
  }, [speakers, current?.speaker_id]);

  useEffect(() => {
    if (selected != null && !lectures.some((lecture) => lecture.id === selected)) {
      setSelected(lectures[0]?.id ?? null);
    }
  }, [lectures, selected]);

  useEffect(() => {
    if (focusId != null) setSelected(focusId);
  }, [focusId, focusToken]);

  useEffect(() => {
    setPicked(current?.speaker_id ?? null);
    setNote("");
    setConfirming(false);
  }, [current?.id, current?.speaker_id]);

  useEffect(() => {
    if (selected == null) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelected(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

  if (!lectures.length) {
    return (
      <div className={empty}>Fila de revisão vazia. Quando o match da foto ficar duvidoso, a aula aparece aqui.</div>
    );
  }

  const confidence =
    current?.speaker_confidence == null
      ? "—"
      : current.speaker_confidence <= 1
        ? `${Math.round(current.speaker_confidence * 100)}%`
        : String(current.speaker_confidence);
  const pending = confirming ? (speakers.find((item) => item.id === picked) ?? null) : null;

  function submitSpeaker() {
    if (!current || !pending || busy) return;
    const lectureId = current.id;
    const speakerId = pending.id;
    setBusy(true);
    setNote("");
    void api
      .assignSpeaker(lectureId, speakerId)
      .then(() => api.continueLecture(lectureId))
      .then(async () => {
        setConfirming(false);
        setNote("Palestrante confirmado. O PDF está sendo gerado.");
        await onChange();
      })
      .catch((err) => setNote(err instanceof Error ? err.message : "Falha ao confirmar"))
      .finally(() => setBusy(false));
  }

  return (
    <div className="flex min-h-0 flex-1 gap-3">
      <section className={`flex min-h-0 min-w-0 flex-1 flex-col ${current ? "max-lg:hidden" : ""}`}>
        <div className="mb-3 flex items-center gap-2">
          <input
            className={searchInput}
            value={query}
            placeholder="Buscar vídeo, pasta ou sugestão"
            onChange={(event) => setQuery(event.target.value)}
          />
          {query ? (
            <button className={btn} onClick={() => setQuery("")}>
              limpar
            </button>
          ) : null}
        </div>
        <p className="m-0 mb-2 text-[11px] text-faint">
          {filtered.length} de {lectures.length} · clique para confirmar o palestrante
        </p>
        <div className="min-h-0 flex-1 overflow-auto rounded-xl border border-line bg-panel">
          {filtered.length === 0 ? (
            <div className="px-4 py-10 text-center text-muted">Nenhuma aula nesta busca.</div>
          ) : (
            <ul className="m-0 list-none p-1.5">
              {filtered.map((lecture) => {
                const on = lecture.id === current?.id;
                return (
                  <li key={lecture.id}>
                    <button type="button" className={listRow(on)} aria-current={on ? "true" : undefined} onClick={() => setSelected(lecture.id)}>
                      <div className="min-w-0 flex-1">
                        <div className="truncate font-medium text-fg">{lecture.video_filename}</div>
                        <div className="truncate text-xs text-muted">
                          {lecture.lecture_date || "sem data"} · {lecture.speaker_name || "sem sugestão"}
                        </div>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </section>
      {current ? (
        <aside className={sidePanel}>
          <header className="flex items-start gap-3 border-b border-line px-4 py-3">
            <button className={`${btn} lg:hidden`} onClick={() => setSelected(null)}>
              voltar
            </button>
            <div className="min-w-0 flex-1">
              <div className="flex items-start justify-between gap-2">
                <h2 className="m-0 truncate text-sm font-semibold">{current.video_filename}</h2>
                <button className={`${btn} hidden lg:inline`} onClick={() => setSelected(null)} title="Esc">
                  fechar
                </button>
              </div>
              <p className={`${muted} m-0 mt-1 text-xs`}>confiança {confidence}</p>
            </div>
          </header>
          <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
            {current.frame_url ? (
              <img className="max-h-56 w-full rounded-lg border border-line bg-black object-contain" src={current.frame_url} alt="frame da aula" />
            ) : (
              <div className={empty}>Sem frame</div>
            )}
            <p className={`${muted} text-xs`}>{current.identification_note || "Confirme quem aparece no vídeo."}</p>
            <h3 className="m-0 mt-3 mb-2 text-[11px] font-medium tracking-wide text-faint uppercase">Quem é</h3>
            <div className="flex flex-col gap-1">
              {orderedSpeakers.map((speaker) => {
                const suggested = speaker.id === current.speaker_id;
                const on = speaker.id === picked;
                return (
                  <button
                    key={speaker.id}
                    type="button"
                    className={`flex cursor-pointer items-center gap-3 rounded-lg border px-2 py-2 text-left ${
                      on ? "border-accent/70 bg-raised" : "border-transparent hover:bg-raised"
                    }`}
                    onClick={() => setPicked(speaker.id)}
                  >
                    {speaker.photo_url ? (
                      <img className="size-12 rounded-lg border border-line object-cover" src={speaker.photo_url} alt="" />
                    ) : (
                      <div className="size-12 rounded-lg border border-line bg-hover" />
                    )}
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-fg">{speaker.name}</span>
                      <span className="block text-[11px] text-faint">{suggested ? "sugestão do match" : speaker.role || "usar este"}</span>
                    </span>
                  </button>
                );
              })}
            </div>
            {note && !confirming ? <p className="mt-3 rounded-lg bg-raised px-3 py-2 text-xs text-fg">{note}</p> : null}
          </div>
          <footer className="border-t border-line px-4 py-3">
            <button
              className={`${btnPrimary} w-full`}
              disabled={!picked || busy}
              onClick={() => {
                if (!speakers.some((item) => item.id === picked)) return;
                setNote("");
                setConfirming(true);
              }}
            >
              {busy ? "confirmando…" : "confirmar e continuar"}
            </button>
            <p className={`${mono} mt-2 text-center`}>a escolha não dispara sozinha</p>
          </footer>
        </aside>
      ) : (
        <aside className="hidden min-h-0 w-[420px] shrink-0 flex-col justify-center rounded-xl border border-dashed border-line-2 px-6 text-center lg:flex xl:w-[480px]">
          <p className="m-0 text-sm text-fg">Selecione uma aula</p>
          <p className={`${muted} mt-1`}>Compare o frame com a foto e confirme. Esc fecha.</p>
        </aside>
      )}
      {current && pending ? (
        <ConfirmDialog
          eyebrow="Continuar o PDF"
          title={pending.name}
          description={`${current.video_filename} segue com este palestrante. O PDF é gerado em seguida.`}
          busy={busy}
          note={note}
          onCancel={() => {
            if (!busy) setConfirming(false);
          }}
          onConfirm={submitSpeaker}
        >
          <ConfirmPair
            leftSrc={current.frame_url}
            leftLabel="vídeo"
            rightSrc={pending.photo_url}
            rightLabel={pending.id === current.speaker_id ? "sugestão" : pending.role || "cadastro"}
          />
        </ConfirmDialog>
      ) : null}
    </div>
  );
}
