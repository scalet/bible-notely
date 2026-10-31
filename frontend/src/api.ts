export type Speaker = {
  id: number;
  name: string;
  role: string;
  photo_path: string;
  active: boolean;
  lecture_count: number;
  photo_url: string | null;
  photo_note?: string;
  face_ready?: boolean;
};

export type Lecture = {
  id: number;
  drive_file_id: string;
  drive_folder_id: string;
  drive_path: string;
  video_filename: string;
  transcript_filename: string;
  summary_filename: string;
  lecture_date: string | null;
  speaker_id: number | null;
  speaker_name: string | null;
  speaker_confidence: number | null;
  identification_status: string;
  identification_note: string;
  frame_url: string | null;
  summary_text: string;
  warning?: string;
  video_size?: number;
  transcribed: boolean;
  summarized: boolean;
  uploaded: boolean;
  youtube_id?: string;
  youtube_url?: string;
  youtube_title?: string;
  youtube_uploaded_at?: string | null;
  status: string;
  error: string;
  updated_at: string;
};

export type Settings = {
  google_connected: boolean;
  google_account?: string;
  google_drive_folder_id: string;
  google_client_configured: boolean;
  ai_provider: string;
  ai_model: string;
  providers: Record<string, boolean>;
  whisper_model: string;
  whisper_language: string;
  speakers_photo_dir?: string;
  youtube_connected?: boolean;
  youtube_upload_folder_id?: string;
  youtube_upload_path?: string;
  youtube_scope_eligible?: number;
  youtube_scope_pending?: number;
  youtube_scope_uploaded?: number;
};

export type YouTubeFolder = {
  id: string;
  name: string;
  path: string;
};

export type YouTubeBrowse = {
  parent_id: string;
  parent_path: string;
  selection_folder_id: string;
  selection_path: string;
  stats: { eligible: number; pending_youtube: number; on_youtube: number };
  folders: YouTubeFolder[];
};

export type Pipeline = {
  running: boolean;
  current_lecture_id: number | null;
  current_filename: string | null;
  stage: string | null;
  message: string;
  progress?: number | null;
  auto?: boolean;
  pause_requested?: boolean;
  prefetch_filename?: string | null;
  prefetch_progress?: number | null;
  processed_today: number;
  work_free_bytes?: number;
  drive_video_count?: number | null;
};

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
  settings: () => req<Settings>("/api/settings"),
  saveSettings: (body: {
    ai_provider?: string;
    google_drive_folder_id?: string;
    youtube_upload_folder_id?: string;
    youtube_upload_path?: string;
  }) =>
    req<Settings>("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  youtubeBrowse: (parentId?: string, parentPath?: string) => {
    const params = new URLSearchParams();
    if (parentId) params.set("parent_id", parentId);
    if (parentPath) params.set("parent_path", parentPath);
    const q = params.toString();
    return req<YouTubeBrowse>(`/api/youtube/browse${q ? `?${q}` : ""}`);
  },
  driveAuth: (origin: string) =>
    req<{ url: string }>(`/api/drive/auth?origin=${encodeURIComponent(origin)}`),
  speakers: () => req<Speaker[]>("/api/speakers"),
  createSpeaker: (body: { name: string; role: string; active: boolean; file: File }) => {
    const data = new FormData();
    data.append("name", body.name);
    data.append("role", body.role);
    data.append("active", body.active ? "true" : "false");
    data.append("file", body.file);
    return req<Speaker>("/api/speakers", { method: "POST", body: data });
  },
  updateSpeaker: (id: number, body: { name: string; role: string; active: boolean }) =>
    req<Speaker>(`/api/speakers/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteSpeaker: (id: number) => req<{ ok: boolean }>(`/api/speakers/${id}`, { method: "DELETE" }),
  uploadPhoto: async (id: number, file: File) => {
    const data = new FormData();
    data.append("file", file);
    return req<Speaker>(`/api/speakers/${id}/photo`, { method: "POST", body: data });
  },
  importPhotos: () =>
    req<{ created: number; updated: number; skipped: number; errors: string[]; directory: string }>(
      "/api/speakers/import-photos",
      { method: "POST" },
    ),
  lectures: (status?: string) =>
    req<Lecture[]>(status ? `/api/lectures?status=${status}` : "/api/lectures"),
  review: () => req<Lecture[]>("/api/review"),
  assignSpeaker: (lectureId: number, speakerId: number) =>
    req<Lecture>(`/api/lectures/${lectureId}/assign-speaker`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ speaker_id: speakerId }),
    }),
  excludeLecture: (id: number) => req<Lecture>(`/api/lectures/${id}/exclude`, { method: "POST" }),
  restoreLecture: (id: number) => req<Lecture>(`/api/lectures/${id}/restore`, { method: "POST" }),
  deleteDownload: (id: number) =>
    req<{ removed: boolean }>(`/api/lectures/${id}/download`, { method: "DELETE" }),
  pipeline: () => req<Pipeline>("/api/pipeline"),
  start: () => req<{ ok: boolean }>("/api/pipeline/start", { method: "POST" }),
  pause: () => req<{ ok: boolean }>("/api/pipeline/pause", { method: "POST" }),
  cancel: () => req<{ ok: boolean }>("/api/pipeline/cancel", { method: "POST" }),
  sync: () => req<{ created: number }>("/api/pipeline/sync", { method: "POST" }),
  run: (max = 1) => req<{ ok: boolean }>(`/api/pipeline/run?max_items=${max}`, { method: "POST" }),
  continueLecture: (id: number, fromStage?: "transcribe" | "summarize" | "upload") =>
    req<{ ok: boolean }>(`/api/pipeline/continue/${id}${fromStage ? `?from_stage=${fromStage}` : ""}`, {
      method: "POST",
    }),
  transcript: async (id: number) => {
    const res = await fetch(`/api/lectures/${id}/transcript`);
    if (!res.ok) throw new Error("Transcrição ainda não disponível");
    return res.text();
  },
};
