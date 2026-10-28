import { useEffect, useMemo, useState, type ReactNode } from "react";
import { ConfirmDialog } from "../ConfirmDialog";
import { api, type Speaker } from "../api";
import { btn, btnPrimary, chip, dangerBtn, input, label, listRow, mono, muted, pill, searchInput, sidePanel } from "../ui";

const FILTERS: { id: string; label: string; match: (speaker: Speaker) => boolean }[] = [
  { id: "all", label: "Todos", match: () => true },
  { id: "face", label: "Sem rosto", match: (speaker) => !speaker.face_ready },
  { id: "off", label: "Inativos", match: (speaker) => !speaker.active },
];

export function Speakers({
  speakers,
  photoDir,
  onChange,
}: {
  speakers: Speaker[];
  photoDir?: string;
  onChange: () => Promise<void>;
}) {
  const [selected, setSelected] = useState<number | "new" | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const activeFilter = FILTERS.find((item) => item.id === filter) ?? FILTERS[0];
  const current = typeof selected === "number" ? speakers.find((speaker) => speaker.id === selected) ?? null : null;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return speakers.filter((speaker) => {
      if (!activeFilter.match(speaker)) return false;
      if (!q) return true;
      return [speaker.name, speaker.role, speaker.photo_note].filter(Boolean).some((value) => String(value).toLowerCase().includes(q));
    });
  }, [speakers, query, activeFilter]);

  useEffect(() => {
    if (typeof selected === "number" && !speakers.some((speaker) => speaker.id === selected)) setSelected(null);
  }, [speakers, selected]);

  useEffect(() => {
    if (selected == null) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelected(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

  const open = selected != null;

  return (
    <div className="flex min-h-0 flex-1 gap-3">
      <section className={`flex min-h-0 min-w-0 flex-1 flex-col ${open ? "max-lg:hidden" : ""}`}>
        <div className="mb-3 flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <input
              className={searchInput}
              value={query}
              placeholder="Buscar nome ou função"
              onChange={(event) => setQuery(event.target.value)}
            />
            <button className={btnPrimary} onClick={() => setSelected("new")}>
              novo
            </button>
          </div>
          <div className="flex gap-1.5 overflow-x-auto pb-0.5">
            {FILTERS.map((item) => {
              const count = speakers.filter(item.match).length;
              const on = item.id === filter;
              return (
                <button key={item.id} className={chip(on)} onClick={() => setFilter(item.id)}>
                  {item.label}
                  <span className={on ? "text-ink/60" : "text-faint"}> {count}</span>
                </button>
              );
            })}
          </div>
          <p className="m-0 text-[11px] text-faint">
            {filtered.length} de {speakers.length} · clique na pessoa para manter
          </p>
        </div>
        <div className="min-h-0 flex-1 overflow-auto rounded-xl border border-line bg-panel">
          {speakers.length === 0 ? (
            <div className="px-4 py-10 text-center text-muted">Nenhum palestrante. Cadastre um rosto antes de processar.</div>
          ) : filtered.length === 0 ? (
            <div className="px-4 py-10 text-center text-muted">Ninguém neste filtro.</div>
          ) : (
            <ul className="m-0 list-none p-1.5">
              {filtered.map((speaker) => {
                const on = speaker.id === current?.id;
                return (
                  <li key={speaker.id}>
                    <button type="button" className={listRow(on)} aria-current={on ? "true" : undefined} onClick={() => setSelected(speaker.id)}>
                      {speaker.photo_url ? (
                        <img className="size-10 shrink-0 rounded-lg border border-line object-cover" src={speaker.photo_url} alt="" />
                      ) : (
                        <div className="size-10 shrink-0 rounded-lg border border-line bg-hover" />
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="truncate font-medium text-fg">{speaker.name}</div>
                        <div className="truncate text-xs text-muted">
                          {speaker.role || "sem função"} · {speaker.lecture_count} aulas
                        </div>
                      </div>
                      <span className={`${pill(speaker.face_ready ? "ok" : speaker.active ? "warn" : "")} mt-0.5 shrink-0`}>
                        <span className="size-1.5 rounded-full bg-current" />
                        {!speaker.active ? "inativo" : speaker.face_ready ? "rosto ok" : "sem rosto"}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </section>
      {selected === "new" ? (
        <CreatePanel photoDir={photoDir} onClose={() => setSelected(null)} onChange={onChange} />
      ) : (
        <SpeakerPanel speaker={current} onClose={() => setSelected(null)} onChange={onChange} />
      )}
    </div>
  );
}

function CreatePanel({
  photoDir,
  onClose,
  onChange,
}: {
  photoDir?: string;
  onClose: () => void;
  onChange: () => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [role, setRole] = useState("");
  const [photo, setPhoto] = useState<File | null>(null);
  const [preview, setPreview] = useState("");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");

  function pickPhoto(file: File | null) {
    setPhoto(file);
    setPreview((current) => {
      if (current) URL.revokeObjectURL(current);
      return file ? URL.createObjectURL(file) : "";
    });
  }

  return (
    <aside className={sidePanel}>
      <PanelHeader title="Novo palestrante" onClose={onClose} />
      <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
        <p className={`${muted} mt-0 text-xs`}>
          A foto vai para <span className={mono}>{photoDir || "data/speakers/fotos"}</span>. O reconhecimento é local; a IA não vê a imagem.
        </p>
        <Field label="Nome">
          <input className={input} value={name} onChange={(event) => setName(event.target.value)} placeholder="Nome completo" />
        </Field>
        <Field label="Função / igreja">
          <input className={input} value={role} onChange={(event) => setRole(event.target.value)} placeholder="opcional" />
        </Field>
        <Field label="Foto">
          <input
            className={input}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            onChange={(event) => pickPhoto(event.target.files?.[0] ?? null)}
          />
          {preview ? <img className="mt-2 size-24 rounded-xl border border-line object-cover" src={preview} alt="prévia da foto" /> : null}
        </Field>
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            className={btnPrimary}
            disabled={Boolean(busy) || !photo || !name.trim()}
            onClick={() => {
              const file = photo;
              if (!file) return;
              void run(setBusy, setNote, "save", async () => {
                const created = await api.createSpeaker({ name: name.trim(), role, active: true, file });
                setName("");
                setRole("");
                pickPhoto(null);
                await onChange();
                return created.face_ready
                  ? "Palestrante cadastrado. Rosto pronto para o match."
                  : `Cadastrado, mas ${created.photo_note || "sem rosto detectado"}.`;
              });
            }}
          >
            {busy === "save" ? "cadastrando…" : "cadastrar"}
          </button>
          <button
            className={btn}
            disabled={Boolean(busy)}
            onClick={() =>
              void run(setBusy, setNote, "import", async () => {
                const result = await api.importPhotos();
                await onChange();
                const errors = result.errors.length ? ` · ${result.errors.length} erro(s)` : "";
                return `Pasta ${result.directory}: ${result.created} novos, ${result.updated} atualizados, ${result.skipped} iguais${errors}`;
              })
            }
          >
            {busy === "import" ? "importando…" : "importar pasta"}
          </button>
        </div>
        <p className={`${muted} text-xs`}>
          Arquivo <span className={mono}>João Silva.jpg</span>. Função opcional: <span className={mono}>João Silva | pastor.jpg</span>.
        </p>
        {note ? <p className="rounded-lg bg-raised px-3 py-2 text-xs text-fg">{note}</p> : null}
      </div>
    </aside>
  );
}

function SpeakerPanel({
  speaker,
  onClose,
  onChange,
}: {
  speaker: Speaker | null;
  onClose: () => void;
  onChange: () => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [role, setRole] = useState("");
  const [active, setActive] = useState(true);
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    setName(speaker?.name ?? "");
    setRole(speaker?.role ?? "");
    setActive(speaker?.active ?? true);
    setNote("");
    setBusy("");
    setConfirmDelete(false);
  }, [speaker?.id, speaker?.name, speaker?.role, speaker?.active]);

  if (!speaker) {
    return (
      <aside className="hidden min-h-0 w-[420px] shrink-0 flex-col justify-center rounded-xl border border-dashed border-line-2 px-6 text-center lg:flex xl:w-[480px]">
        <p className="m-0 text-sm text-fg">Selecione um palestrante</p>
        <p className={`${muted} mt-1`}>Foto, função e exclusão ficam neste painel. Esc fecha.</p>
      </aside>
    );
  }

  const dirty = name !== speaker.name || role !== speaker.role || active !== speaker.active;

  return (
    <aside className={sidePanel}>
      <PanelHeader title={speaker.name} subtitle={speaker.role || "sem função"} onClose={onClose} />
      <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
        <div className="flex items-start gap-3">
          {speaker.photo_url ? (
            <img className="size-24 rounded-xl border border-line object-cover" src={speaker.photo_url} alt={speaker.name} />
          ) : (
            <div className="size-24 rounded-xl border border-line bg-hover" />
          )}
          <div>
            <span className={pill(speaker.face_ready ? "ok" : "warn")}>
              <span className="size-1.5 rounded-full bg-current" />
              {speaker.face_ready ? "rosto pronto" : "sem rosto"}
            </span>
            <p className={`${muted} text-xs`}>{speaker.photo_note || (speaker.face_ready ? "Embedding local ok." : "Troque a foto por um retrato nítido.")}</p>
            <p className={`${mono} m-0`}>{speaker.lecture_count} aulas</p>
          </div>
        </div>
        <Field label="Nome">
          <input className={input} value={name} onChange={(event) => setName(event.target.value)} />
        </Field>
        <Field label="Função / igreja">
          <input className={input} value={role} onChange={(event) => setRole(event.target.value)} placeholder="opcional" />
        </Field>
        <label className="mt-3 flex items-center gap-2 text-xs text-fg">
          <input type="checkbox" checked={active} onChange={(event) => setActive(event.target.checked)} />
          ativo no reconhecimento
        </label>
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            className={btnPrimary}
            disabled={!dirty || !name.trim() || Boolean(busy)}
            onClick={() =>
              void run(setBusy, setNote, "save", async () => {
                await api.updateSpeaker(speaker.id, { name: name.trim(), role, active });
                await onChange();
                return "Cadastro atualizado.";
              })
            }
          >
            {busy === "save" ? "salvando…" : "salvar"}
          </button>
          <label className={busy ? `${btn} opacity-50` : btn}>
            {busy === "photo" ? "enviando…" : "trocar foto"}
            <input
              type="file"
              accept="image/jpeg,image/png,image/webp"
              hidden
              disabled={Boolean(busy)}
              onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = "";
                if (!file) return;
                void run(setBusy, setNote, "photo", async () => {
                  const updated = await api.uploadPhoto(speaker.id, file);
                  await onChange();
                  return updated.face_ready ? "Foto atualizada. Rosto pronto." : `Foto gravada, mas ${updated.photo_note || "sem rosto"}.`;
                });
              }}
            />
          </label>
        </div>
        {note && !confirmDelete ? <p className="mt-3 rounded-lg bg-raised px-3 py-2 text-xs text-fg">{note}</p> : null}
        <section className="mt-4 border-t border-line pt-3">
          <h3 className="m-0 mb-2 text-[11px] font-medium tracking-wide text-faint uppercase">Perigo</h3>
          <button
            className={dangerBtn}
            disabled={Boolean(busy)}
            onClick={() => {
              setNote("");
              setConfirmDelete(true);
            }}
          >
            {busy === "delete" ? "removendo…" : "excluir palestrante"}
          </button>
        </section>
      </div>
      {confirmDelete ? (
        <ConfirmDialog
          tone="danger"
          eyebrow="Remover cadastro"
          title={speaker.name}
          description="O palestrante sai do reconhecimento. As aulas já processadas continuam no Drive."
          busy={busy === "delete"}
          note={note}
          confirmLabel="excluir"
          busyLabel="removendo…"
          onCancel={() => {
            if (!busy) setConfirmDelete(false);
          }}
          onConfirm={() => {
            void run(setBusy, setNote, "delete", async () => {
              await api.deleteSpeaker(speaker.id);
              await onChange();
              setConfirmDelete(false);
              return "Palestrante removido.";
            });
          }}
        >
          <div className="mt-4 flex items-center gap-3">
            {speaker.photo_url ? (
              <img className="size-16 rounded-xl border border-line object-cover" src={speaker.photo_url} alt="" />
            ) : (
              <div className="size-16 rounded-xl border border-dashed border-line-2 bg-hover" />
            )}
            <p className={`${mono} m-0`}>
              {speaker.lecture_count === 1 ? "1 aula" : `${speaker.lecture_count} aulas`}
            </p>
          </div>
        </ConfirmDialog>
      ) : null}
    </aside>
  );
}

function PanelHeader({ title, subtitle, onClose }: { title: string; subtitle?: string; onClose: () => void }) {
  return (
    <header className="flex items-start gap-3 border-b border-line px-4 py-3">
      <button className={`${btn} lg:hidden`} onClick={onClose}>
        voltar
      </button>
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <h2 className="m-0 truncate text-sm font-semibold" title={title}>
            {title}
          </h2>
          <button className={`${btn} hidden lg:inline`} onClick={onClose} title="Esc">
            fechar
          </button>
        </div>
        {subtitle ? <p className={`${muted} m-0 mt-1 text-xs`}>{subtitle}</p> : null}
      </div>
    </header>
  );
}

function Field({ label: text, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mt-3 flex flex-col gap-1.5">
      <span className={label}>{text}</span>
      {children}
    </div>
  );
}

async function run(
  setBusy: (value: string) => void,
  setNote: (value: string) => void,
  key: string,
  action: () => Promise<string>,
) {
  setBusy(key);
  setNote("");
  try {
    setNote(await action());
  } catch (err) {
    setNote(err instanceof Error ? err.message : "Falha na ação");
  } finally {
    setBusy("");
  }
}
