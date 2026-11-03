import { useEffect, useState } from "react";
import { api, type LyricPending } from "../api";
import { btn, btnPrimary, empty, listRow, mono, muted, sidePanel } from "../ui";

function deadlineLabel(item: LyricPending) {
  if (item.status === "timeout") return "prazo esgotado — o vídeo seguiu sem esta letra";
  if (!item.deadline_at) return "sem prazo";
  const when = new Date(item.deadline_at);
  if (Number.isNaN(when.getTime())) return "prazo em Ajustes";
  return `espera até ${when.toLocaleString("pt-BR")}`;
}

export function Lyrics({ onChange }: { onChange: () => Promise<void> }) {
  const [items, setItems] = useState<LyricPending[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [hymn, setHymn] = useState("");
  const [title, setTitle] = useState("");
  const [lyrics, setLyrics] = useState("");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  async function load() {
    const rows = await api.lyricPendings();
    setItems(rows);
    setSelected((current) => (current != null && rows.some((row) => row.id === current) ? current : (rows[0]?.id ?? null)));
  }

  useEffect(() => {
    void load().catch((err) => setError(err instanceof Error ? err.message : "Falha ao carregar"));
  }, []);

  useEffect(() => {
    setHymn("");
    setTitle("");
    setLyrics("");
    setNote("");
    setError("");
  }, [selected]);

  useEffect(() => {
    if (selected == null) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelected(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

  const current = items.find((item) => item.id === selected) ?? null;

  async function answer(action: "hymn" | "lyrics" | "skip") {
    if (!current) return;
    setBusy(action);
    setError("");
    setNote("");
    try {
      await api.resolveLyric(current.id, { action, query: hymn, lyrics, title });
      setNote(
        action === "skip"
          ? "Segue sem letra. Na próxima vez essa música não pergunta de novo."
          : "Guardado na biblioteca. Na próxima vez a música entra sozinha.",
      );
      await load();
      await onChange();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Falha ao salvar");
    } finally {
      setBusy("");
    }
  }

  if (!items.length) {
    return (
      <div className={empty}>
        Nenhuma música sem letra. Quando o Cantor Cristão e a biblioteca não reconhecerem um cântico, a pergunta
        aparece aqui, com o trecho de áudio.
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 gap-3">
      <section className={`flex min-h-0 min-w-0 flex-1 flex-col ${current ? "max-lg:hidden" : ""}`}>
        <p className="m-0 mb-2 text-[11px] text-faint">
          O vídeo não fica preso para sempre. O prazo está em Ajustes → Vídeo. 0 horas não espera.
        </p>
        <div className="min-h-0 flex-1 overflow-auto rounded-xl border border-line bg-panel">
          <ul className="m-0 list-none p-1.5">
            {items.map((item) => (
              <li key={item.id}>
                <button type="button" className={listRow(item.id === selected)} onClick={() => setSelected(item.id)}>
                  <span className="min-w-0 flex-1">
                    <span className="block font-medium text-fg">{item.lecture_filename || `Aula ${item.lecture_id}`}</span>
                    <span className={`block text-xs ${mono}`}>
                      {item.start_label} – {item.end_label}
                    </span>
                    <span className="block text-xs text-muted">{deadlineLabel(item)}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </section>
      {current ? (
        <aside className={sidePanel}>
          <header className="flex items-start gap-3 border-b border-line px-4 py-3">
            <button className={`${btn} lg:hidden`} onClick={() => setSelected(null)}>
              voltar
            </button>
            <div className="min-w-0 flex-1">
              <h2 className="m-0 truncate text-sm font-semibold">{current.lecture_filename || `Aula ${current.lecture_id}`}</h2>
              <p className={`${muted} m-0 text-xs`}>
                {current.start_label} – {current.end_label} · {deadlineLabel(current)}
              </p>
            </div>
          </header>
          <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
            <p className="m-0 text-xs text-fg">{current.transcript_excerpt || "A transcrição desse trecho veio vazia."}</p>
            {current.snippet_url ? (
              <audio className="mt-3 w-full" controls preload="metadata" src={current.snippet_url} />
            ) : (
              <p className={`${muted} text-xs`}>Trecho de áudio indisponível.</p>
            )}
            <label className="mt-4 flex flex-col gap-1">
              <span className="text-xs text-muted">Número ou nome do hino</span>
              <input className="w-full rounded-lg border border-line bg-ink px-2.5 py-2 text-fg" value={hymn} onChange={(event) => setHymn(event.target.value)} placeholder="42 ou Grandes coisas" />
            </label>
            <button className={`${btnPrimary} mt-2`} disabled={Boolean(busy) || !hymn.trim()} onClick={() => void answer("hymn")}>
              {busy === "hymn" ? "buscando…" : "usar este hino"}
            </button>
            <label className="mt-4 flex flex-col gap-1">
              <span className="text-xs text-muted">Nome da música</span>
              <input className="w-full rounded-lg border border-line bg-ink px-2.5 py-2 text-fg" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="opcional, se for colar a letra" />
            </label>
            <label className="mt-2 flex flex-col gap-1">
              <span className="text-xs text-muted">Ou cole a letra</span>
              <textarea
                className="min-h-28 w-full rounded-lg border border-line bg-ink px-2.5 py-2 text-fg"
                value={lyrics}
                onChange={(event) => setLyrics(event.target.value)}
                placeholder={"Uma estrofe por bloco, separado por linha em branco."}
              />
            </label>
            <div className="mt-2 flex flex-wrap gap-2">
              <button className={btnPrimary} disabled={Boolean(busy) || lyrics.trim().length < 8} onClick={() => void answer("lyrics")}>
                {busy === "lyrics" ? "salvando…" : "salvar letra"}
              </button>
              <button className={btn} disabled={Boolean(busy)} onClick={() => void answer("skip")}>
                {busy === "skip" ? "salvando…" : "seguir sem letra"}
              </button>
            </div>
            {note ? <p className="mt-3 rounded-lg bg-raised px-3 py-2 text-xs text-fg">{note}</p> : null}
            {error ? <p className="mt-3 rounded-lg bg-raised px-3 py-2 text-xs text-err">{error}</p> : null}
          </div>
        </aside>
      ) : null}
    </div>
  );
}
