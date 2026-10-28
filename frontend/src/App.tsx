import { useEffect, useRef, useState } from "react";
import { api, type Lecture, type Pipeline, type Settings, type Speaker } from "./api";
import { Dashboard } from "./pages/Dashboard";
import { Lectures } from "./pages/Lectures";
import { Review } from "./pages/Review";
import { SettingsPage } from "./pages/Settings";
import { Speakers } from "./pages/Speakers";
import { btnGhost, btnPrimary, pill } from "./ui";

const NAV = [
  { id: "home", label: "Painel" },
  { id: "lectures", label: "Aulas" },
  { id: "speakers", label: "Palestrantes" },
  { id: "review", label: "Revisar" },
  { id: "settings", label: "Ajustes" },
] as const;

type Page = (typeof NAV)[number]["id"];

export default function App() {
  const [page, setPage] = useState<Page>("home");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [pipeline, setPipeline] = useState<Pipeline | null>(null);
  const [speakers, setSpeakers] = useState<Speaker[]>([]);
  const [lectures, setLectures] = useState<Lecture[]>([]);
  const [review, setReview] = useState<Lecture[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [focus, setFocus] = useState<{ page: Page; id: number; token: number } | null>(null);
  const booted = useRef(false);
  const lastFull = useRef(0);
  const running = useRef(false);

  async function refreshLive() {
    const [p, lec] = await Promise.allSettled([api.pipeline(), api.lectures()]);
    if (p.status === "fulfilled") {
      setPipeline(p.value);
      running.current = p.value.running;
    }
    if (lec.status === "fulfilled") setLectures(lec.value);
  }

  async function refresh() {
    lastFull.current = Date.now();
    const [s, p, sp, lec, rev] = await Promise.allSettled([
      api.settings(),
      api.pipeline(),
      api.speakers(),
      api.lectures(),
      api.review(),
    ]);
    if (s.status === "fulfilled") setSettings(s.value);
    if (p.status === "fulfilled") {
      setPipeline(p.value);
      running.current = p.value.running;
    }
    if (sp.status === "fulfilled") {
      setSpeakers(sp.value);
      if (!booted.current) {
        booted.current = true;
        if (sp.value.length === 0) setPage("speakers");
      }
    }
    if (lec.status === "fulfilled") setLectures(lec.value);
    if (rev.status === "fulfilled") setReview(rev.value);
    const failed = [s, p, sp, lec, rev].find((item) => item.status === "rejected");
    setError(failed && failed.status === "rejected" ? String(failed.reason instanceof Error ? failed.reason.message : failed.reason) : "");
  }

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const drive = params.get("drive");
    if (drive === "error") {
      setNotice(params.get("reason") || "Google recusou a conexão. Clique em conectar de novo.");
      setPage("settings");
    } else if (drive === "connected") {
      setNotice("Google conectado.");
      setPage("settings");
    }
    if (drive) window.history.replaceState({}, "", window.location.pathname);
    void refresh();
    // Só pipeline e aulas mudam sozinhos; o resto é recarregado após ações do usuário.
    const id = setInterval(() => {
      if (document.visibilityState !== "visible") return;
      if (running.current) void refreshLive();
      else if (Date.now() - lastFull.current > 120000) void refresh();
    }, 15000);
    const onVisible = () => {
      if (document.visibilityState === "visible" && Date.now() - lastFull.current > 30000) void refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  async function runNext() {
    let failure = "";
    try {
      await api.run(1);
    } catch (err) {
      failure = err instanceof Error ? err.message : "Falha ao processar o próximo";
    }
    await refresh();
    if (failure) setError(failure);
  }

  async function startAuto() {
    try {
      await api.start();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Falha ao iniciar");
    }
    await refresh();
  }

  async function pauseAuto() {
    try {
      await api.pause();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Falha ao pausar");
    }
    await refresh();
  }

  const paused = !pipeline?.running && (pipeline?.message ?? "").startsWith("Pausado");

  const pageLabel = NAV.find((n) => n.id === page)?.label;

  return (
    <div className="flex h-full min-h-0 flex-col bg-ink text-[13.5px] text-fg md:grid md:grid-cols-[232px_1fr]">
      <aside className="flex shrink-0 flex-col gap-4 border-b border-line bg-panel p-3 md:min-h-0 md:overflow-y-auto md:border-b-0 md:border-r">
        <div className="flex items-center gap-2.5 px-2 pt-1">
          <div className="size-[22px] rounded-md bg-linear-to-br from-accent to-accent-2 shadow-[0_0_0_1px_rgba(255,255,255,0.08)]" />
          <div>
            <h1 className="m-0 text-sm font-semibold">bible-notely</h1>
            <span className="block text-[11px] font-normal text-muted">pipeline de aulas</span>
          </div>
        </div>
        <nav className="flex gap-1 overflow-x-auto md:flex-col">
          {NAV.map((item) => (
            <button
              key={item.id}
              className={`cursor-pointer rounded-lg border-0 px-2.5 py-2 text-left whitespace-nowrap ${
                page === item.id
                  ? "bg-raised text-fg shadow-[inset_0_0_0_1px_var(--color-line)]"
                  : "bg-transparent text-muted hover:bg-raised hover:text-fg"
              }`}
              onClick={() => setPage(item.id)}
            >
              {item.label}
              {item.id === "review" && review.length > 0 ? ` (${review.length})` : ""}
            </button>
          ))}
        </nav>
        <div className="mt-auto hidden rounded-[10px] border border-line bg-ink p-2.5 text-xs text-muted md:block">
          {pipeline?.running ? (
            <>
              <div className={pill("accent")}>
                <span className="size-1.5 rounded-full bg-current" /> processando
              </div>
              <div className="mt-2">{pipeline.message}</div>
            </>
          ) : (
            <>
              Drive {settings?.google_connected ? settings.google_account || "conectado" : "offline"} · {settings?.ai_provider}
            </>
          )}
        </div>
      </aside>
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <header className="flex h-12 items-center justify-between border-b border-line bg-ink/90 px-5 backdrop-blur-md">
          <div className="text-muted">
            bible-notely / <b className="font-semibold text-fg">{pageLabel}</b>
          </div>
          <div className="flex gap-2">
            <button className={btnGhost} onClick={() => void refresh()}>
              atualizar
            </button>
            {pipeline?.running && pipeline.auto ? (
              <button className={btnPrimary} disabled={pipeline.pause_requested} onClick={() => void pauseAuto()}>
                {pipeline.pause_requested ? "pausando…" : "pausar"}
              </button>
            ) : (
              <button
                className={btnPrimary}
                disabled={pipeline?.running || speakers.length === 0}
                title={speakers.length === 0 ? "Cadastre um professor antes" : undefined}
                onClick={() => void startAuto()}
              >
                {paused ? "continuar" : "iniciar automático"}
              </button>
            )}
            <button className={btnGhost} disabled={pipeline?.running || speakers.length === 0} onClick={() => void runNext()}>
              processar próximo
            </button>
          </div>
        </header>
        <div
          className={`flex min-h-0 flex-1 flex-col p-5 ${page === "home" ? "overflow-auto" : "overflow-hidden"}`}
        >
          {notice ? <p className={`${pill(notice.startsWith("Google conectado") ? "ok" : "err")} mb-3`}>{notice}</p> : null}
          {error ? <p className={pill("err")}>{error}</p> : null}
          {!pipeline?.running && pipeline?.stage === "failed" ? (
            <p className={`${pill("err")} mb-3`}>{pipeline.message}</p>
          ) : null}
          {page === "home" && (
            <Dashboard
              settings={settings}
              pipeline={pipeline}
              lectures={lectures}
              review={review}
              onOpenLecture={(id) => {
                setFocus({ page: "lectures", id, token: Date.now() });
                setPage("lectures");
              }}
              onOpenReview={(id) => {
                setFocus({ page: "review", id, token: Date.now() });
                setPage("review");
              }}
            />
          )}
          {page === "lectures" && (
            <Lectures
              lectures={lectures}
              speakers={speakers}
              onChange={refresh}
              focusId={focus?.page === "lectures" ? focus.id : null}
              focusToken={focus?.page === "lectures" ? focus.token : 0}
            />
          )}
          {page === "speakers" && (
            <Speakers speakers={speakers} photoDir={settings?.speakers_photo_dir} onChange={refresh} />
          )}
          {page === "review" && (
            <Review
              lectures={review}
              speakers={speakers}
              onChange={refresh}
              focusId={focus?.page === "review" ? focus.id : null}
              focusToken={focus?.page === "review" ? focus.token : 0}
            />
          )}
          {page === "settings" && (
            <SettingsPage
              settings={settings}
              speakersReady={speakers.length > 0}
              onChange={refresh}
              onOpenSpeakers={() => setPage("speakers")}
            />
          )}
        </div>
      </main>
    </div>
  );
}
