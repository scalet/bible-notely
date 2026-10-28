import { useEffect, useState } from "react";
import { api, type Settings } from "../api";
import { btn, btnPrimary, card, empty, input, label, listRow, mono, muted, pill, sidePanel } from "../ui";

const PROVIDERS = [
  { id: "gemini", label: "Gemini" },
  { id: "grok", label: "Grok" },
  { id: "claude", label: "Claude" },
  { id: "copilot", label: "Copilot" },
];

const SECTIONS = [
  { id: "drive", label: "Google Drive", hint: "conexão e pasta raiz" },
  { id: "ai", label: "Provedor de IA", hint: "quem escreve o resumo" },
  { id: "local", label: "Ambiente", hint: "whisper, modelo e fotos" },
] as const;

type SectionId = (typeof SECTIONS)[number]["id"];

export function SettingsPage({
  settings,
  speakersReady,
  onChange,
  onOpenSpeakers,
}: {
  settings: Settings | null;
  speakersReady: boolean;
  onChange: () => Promise<void>;
  onOpenSpeakers: () => void;
}) {
  const [folder, setFolder] = useState(settings?.google_drive_folder_id || "");
  const [section, setSection] = useState<SectionId | null>(null);
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  useEffect(() => {
    setFolder(settings?.google_drive_folder_id || "");
  }, [settings?.google_drive_folder_id]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSection(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!settings) return <div className={empty}>Carregando ajustes…</div>;

  if (!speakersReady) {
    return (
      <div className={card}>
        <h3 className="m-0 mb-1.5 text-xs font-medium text-muted">Primeiro os professores</h3>
        <p className={muted}>
          As chaves do Google e da IA só entram depois de haver pelo menos um professor cadastrado. A foto é local e
          não usa essas chaves.
        </p>
        <button className={btnPrimary} onClick={onOpenSpeakers}>
          cadastrar professores
        </button>
      </div>
    );
  }

  const current = SECTIONS.find((item) => item.id === section) ?? null;

  async function save(key: string, action: () => Promise<string>) {
    setBusy(key);
    setNote("");
    try {
      setNote(await action());
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Falha ao salvar");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="flex min-h-0 flex-1 gap-3">
      <section className={`flex min-h-0 min-w-0 flex-1 flex-col ${current ? "max-lg:hidden" : ""}`}>
        <p className="m-0 mb-2 text-[11px] text-faint">Clique na seção para ajustar. As chaves continuam só no .env.</p>
        <div className="min-h-0 flex-1 overflow-auto rounded-xl border border-line bg-panel">
          <ul className="m-0 list-none p-1.5">
            {SECTIONS.map((item) => (
              <li key={item.id}>
                <button type="button" className={listRow(item.id === section)} onClick={() => setSection(item.id)}>
                  <span className="min-w-0 flex-1">
                    <span className="block font-medium text-fg">{item.label}</span>
                    <span className="block text-xs text-muted">{item.hint}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </section>
      {section === "drive" ? (
        <aside className={sidePanel}>
          <SectionHeader title="Google Drive" onClose={() => setSection(null)} />
          <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
            <p className={`${muted} mt-0 text-xs`}>
              Chaves no <span className={mono}>.env</span> · pasta no padrão ANO/NroMes-NomeMes/Dia-NroMes/*.mp4
            </p>
            <div className={`${pill(settings.google_connected ? "ok" : "")} my-2.5`}>
              <span className="size-1.5 rounded-full bg-current" />
              {settings.google_connected
                ? `conectado${settings.google_account ? ` · ${settings.google_account}` : ""}`
                : "não conectado"}
            </div>
            <div className="mb-2.5 flex flex-col gap-1.5">
              <label className={label}>ID da pasta raiz</label>
              <input className={input} value={folder} onChange={(event) => setFolder(event.target.value)} placeholder="1abc..." />
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                className={btn}
                disabled={Boolean(busy) || folder === settings.google_drive_folder_id}
                onClick={() =>
                  void save("folder", async () => {
                    await api.saveSettings({ google_drive_folder_id: folder });
                    await onChange();
                    return "Pasta salva.";
                  })
                }
              >
                {busy === "folder" ? "salvando…" : "salvar pasta"}
              </button>
              <button
                className={btnPrimary}
                disabled={!settings.google_client_configured || Boolean(busy)}
                onClick={() =>
                  void save("auth", async () => {
                    const { url } = await api.driveAuth(window.location.origin);
                    window.location.href = url;
                    return "Abrindo o Google…";
                  })
                }
              >
                conectar google
              </button>
            </div>
            {note ? <p className="mt-3 rounded-lg bg-raised px-3 py-2 text-xs text-fg">{note}</p> : null}
          </div>
        </aside>
      ) : section === "ai" ? (
        <aside className={sidePanel}>
          <SectionHeader title="Provedor de IA" onClose={() => setSection(null)} />
          <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
            <p className={`${muted} mt-0 text-xs`}>A troca vale para o próximo resumo. Chaves só no .env.</p>
            <div className="flex flex-col gap-2">
              {PROVIDERS.map((provider) => {
                const ready = settings.providers[provider.id];
                const active = settings.ai_provider === provider.id;
                return (
                  <button
                    key={provider.id}
                    className={`cursor-pointer rounded-lg border bg-ink p-3 text-left disabled:opacity-50 ${
                      active ? "border-accent shadow-[0_0_0_1px_var(--color-accent)]" : "border-line hover:bg-raised"
                    }`}
                    disabled={!ready || Boolean(busy)}
                    onClick={() =>
                      void save("provider", async () => {
                        await api.saveSettings({ ai_provider: provider.id });
                        await onChange();
                        return `Provedor agora é ${provider.label}.`;
                      })
                    }
                  >
                    <b className="mb-1 block">{provider.label}</b>
                    <span className={muted}>{active ? "em uso" : ready ? "chave ok" : "sem chave"}</span>
                  </button>
                );
              })}
            </div>
            <p className={`${mono} mt-3`}>modelo: {settings.ai_model}</p>
            {note ? <p className="mt-3 rounded-lg bg-raised px-3 py-2 text-xs text-fg">{note}</p> : null}
          </div>
        </aside>
      ) : section === "local" ? (
        <aside className={sidePanel}>
          <SectionHeader title="Ambiente" onClose={() => setSection(null)} />
          <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
            <dl className="m-0 grid grid-cols-[110px_1fr] gap-x-3 gap-y-2 text-xs">
              <dt className="text-faint">Whisper</dt>
              <dd className="m-0 text-fg">
                {settings.whisper_model} · {settings.whisper_language}
              </dd>
              <dt className="text-faint">Modelo</dt>
              <dd className="m-0 text-fg">{settings.ai_model}</dd>
              <dt className="text-faint">Fotos</dt>
              <dd className="m-0 break-all text-fg">{settings.speakers_photo_dir || "data/speakers/fotos"}</dd>
              <dt className="text-faint">Cliente Google</dt>
              <dd className="m-0 text-fg">{settings.google_client_configured ? "configurado" : "faltando no .env"}</dd>
            </dl>
            <p className={`${muted} text-xs`}>Estes valores vêm do ambiente. Para mudar, edite o .env e reinicie o serviço.</p>
          </div>
        </aside>
      ) : (
        <aside className="hidden min-h-0 w-[420px] shrink-0 flex-col justify-center rounded-xl border border-dashed border-line-2 px-6 text-center lg:flex xl:w-[480px]">
          <p className="m-0 text-sm text-fg">Selecione uma seção</p>
          <p className={`${muted} mt-1`}>Drive, IA e ambiente ficam separados para não misturar a manutenção.</p>
        </aside>
      )}
    </div>
  );
}

function SectionHeader({ title, onClose }: { title: string; onClose: () => void }) {
  return (
    <header className="flex items-start gap-3 border-b border-line px-4 py-3">
      <button className={`${btn} lg:hidden`} onClick={onClose}>
        voltar
      </button>
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <h2 className="m-0 truncate text-sm font-semibold">{title}</h2>
          <button className={`${btn} hidden lg:inline`} onClick={onClose} title="Esc">
            fechar
          </button>
        </div>
      </div>
    </header>
  );
}