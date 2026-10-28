from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
import unicodedata
from collections.abc import Callable
from datetime import date
from pathlib import Path
from threading import Event

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from backend.app.config import settings
from backend.app.runtime import get_drive_index, get_folder_id, set_drive_index

log = logging.getLogger("bible_notely.google")

SCOPES = ["https://www.googleapis.com/auth/drive"]
PATH_LABEL = "ANO/NroMes-NomeMes/Dia-NroMes"
PATH_RE = re.compile(r"(?:^|/)(\d{4})/(\d{2})-([^/]+)/(\d{2})-(\d{2})$")
YEAR_RE = re.compile(r"^\d{4}$")
MONTH_RE = re.compile(r"^\d{2}-")
DAY_RE = re.compile(r"^\d{2}-\d{2}$")
MONTH_NAMES = {
    1: "janeiro",
    2: "fevereiro",
    3: "março",
    4: "abril",
    5: "maio",
    6: "junho",
    7: "julho",
    8: "agosto",
    9: "setembro",
    10: "outubro",
    11: "novembro",
    12: "dezembro",
}
DISK_MARGIN_BYTES = 512 * 1024 * 1024
_last_state = ""
_account_cache: dict[str, str | float] = {"mtime": 0.0, "label": "", "fail_until": 0.0}
_creds_cache: dict[str, object] = {"mtime": 0.0, "creds": None, "fail_until": 0.0}
_service_cache: dict[str, object] = {"mtime": 0.0, "service": None}
_listed_folders: set[str] = set()
FAIL_TTL_SECONDS = 300


class DriveError(RuntimeError):
    pass


class DriveCancelled(DriveError):
    pass


class DriveIncomplete(DriveError):
    pass


class DriveTransient(DriveError):
    """Falha temporária (rede, 5xx, 429): vale tentar de novo."""


def _note(state: str, level: int = logging.INFO) -> None:
    global _last_state
    if state == _last_state:
        return
    _last_state = state
    log.log(level, "acesso Google: %s", state)


def _http_error(exc: HttpError) -> str:
    status = getattr(exc.resp, "status", "?")
    message = ""
    try:
        payload = json.loads(exc.content.decode())
        message = str((payload.get("error") or {}).get("message") or "")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        message = ""
    return f"HTTP {status}: {message or 'sem detalhe'}"


def _execute(label: str, request):
    try:
        return request.execute(num_retries=3)
    except HttpError as exc:
        detail = _http_error(exc)
        log.error("Google recusou %s: %s", label, detail)
        raise DriveError(f"Google recusou {label}: {detail}") from exc


def credentials() -> Credentials | None:
    path = settings.google_token_path
    if not path.exists():
        _note(f"sem token em {path.name}; a conta ainda não foi conectada", logging.WARNING)
        return None
    mtime = path.stat().st_mtime
    cached = _creds_cache["creds"]
    if cached is not None and _creds_cache["mtime"] == mtime and cached.valid:
        return cached
    if _creds_cache["mtime"] == mtime and time.monotonic() < float(_creds_cache["fail_until"]):
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    except Exception as exc:
        _note(f"não li o token em {path.name}: {type(exc).__name__}", logging.ERROR)
        return None
    if creds and creds.expired and creds.refresh_token:
        log.info("token Google expirado; renovando o acesso")
        try:
            creds.refresh(Request())
        except Exception as exc:
            _note(f"renovação do token falhou: {type(exc).__name__}", logging.ERROR)
            _creds_cache.update(mtime=mtime, creds=None, fail_until=time.monotonic() + FAIL_TTL_SECONDS)
            return None
        path.write_text(creds.to_json(), encoding="utf-8")
        mtime = path.stat().st_mtime
        _account_cache["mtime"] = 0.0
        expiry = creds.expiry.isoformat() if creds.expiry else "sem validade"
        log.info("token Google renovado; expira em %s", expiry)
    if not creds or not creds.valid:
        _note("token Google inválido e sem renovação", logging.WARNING)
        _creds_cache.update(mtime=mtime, creds=None, fail_until=time.monotonic() + FAIL_TTL_SECONDS)
        return None
    _creds_cache.update(mtime=mtime, creds=creds, fail_until=0.0)
    expiry = creds.expiry.isoformat() if creds.expiry else "sem validade"
    _note(f"token OAuth válido; expira em {expiry}")
    return creds


def is_connected() -> bool:
    try:
        return credentials() is not None
    except Exception as exc:
        _note(f"falha ao checar a conta: {type(exc).__name__}", logging.ERROR)
        return False


def _read_account(service) -> str:
    about = _execute("leitura da conta", service.about().get(fields="user(emailAddress,displayName)"))
    user = about.get("user") or {}
    email = user.get("emailAddress") or "e-mail desconhecido"
    name = user.get("displayName") or ""
    return f"{name} <{email}>".strip()


def google_account() -> str:
    """E-mail da conta OAuth, em cache. Não chama o Google a cada poll da tela."""
    path = settings.google_token_path
    if not path.exists() or credentials() is None:
        _account_cache.update(mtime=0.0, label="", fail_until=0.0)
        return ""
    mtime = path.stat().st_mtime
    if _account_cache["mtime"] == mtime and _account_cache["label"]:
        return str(_account_cache["label"])
    if _account_cache["mtime"] == mtime and time.monotonic() < float(_account_cache["fail_until"]):
        return ""
    try:
        label = _read_account(_service(log_missing=False))
    except Exception as exc:
        log.warning("não identifiquei a conta Google: %s", exc)
        _account_cache.update(mtime=mtime, label="", fail_until=time.monotonic() + FAIL_TTL_SECONDS)
        return ""
    _account_cache.update(mtime=mtime, label=label, fail_until=0.0)
    log.info("conta Google em uso: %s (OAuth do usuário, não conta de serviço)", label)
    return label


def _pkce_path() -> Path:
    return settings.google_token_path.with_name("google_oauth_pkce.json")


def _client_config() -> dict:
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }


def _client_flow(*, code_verifier: str | None = None, state: str | None = None) -> Flow:
    kwargs: dict = {
        "redirect_uri": settings.google_redirect_uri,
        "autogenerate_code_verifier": code_verifier is None,
    }
    if code_verifier:
        kwargs["code_verifier"] = code_verifier
    if state:
        kwargs["state"] = state
    return Flow.from_client_config(_client_config(), scopes=SCOPES, **kwargs)


def _allowed_origin(origin: str | None) -> str:
    cleaned = (origin or "").rstrip("/")
    allowed = {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        settings.frontend_origin.rstrip("/"),
    }
    if cleaned in allowed:
        return cleaned
    return settings.frontend_origin.rstrip("/") or "http://localhost:5173"


def login_return_origin() -> str:
    return _allowed_origin(str(_load_pkce().get("return_origin") or ""))


def _save_pkce(state: str, code_verifier: str, return_origin: str) -> None:
    path = _pkce_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "state": state,
                "code_verifier": code_verifier,
                "return_origin": return_origin,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _load_pkce() -> dict:
    path = _pkce_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.error("não li o verificador PKCE em %s", path.name)
        return {}
    return data if isinstance(data, dict) else {}


def auth_url(return_origin: str | None = None) -> str:
    if not settings.google_client_id or not settings.google_client_secret:
        log.error("login Google pedido sem GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET")
        raise DriveError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET ausentes no .env")
    origin = _allowed_origin(return_origin)
    flow = _client_flow()
    url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    if not flow.code_verifier:
        log.error("login Google não gerou code_verifier")
        raise DriveError("Login Google não gerou o verificador PKCE")
    _save_pkce(state, flow.code_verifier, origin)
    log.info(
        "abrindo login Google: redirect=%s retorno=%s escopo=%s pkce=salvo",
        settings.google_redirect_uri,
        origin,
        ",".join(SCOPES),
    )
    return url


def exchange_code(code: str) -> None:
    pending = _load_pkce()
    verifier = str(pending.get("code_verifier") or "")
    if not verifier:
        log.error("troca do código Google sem code_verifier salvo; o login precisa ser reaberto")
        raise DriveError("Login Google perdeu o verificador. Clique em conectar de novo.")
    state = str(pending.get("state") or "") or None
    flow = _client_flow(code_verifier=verifier, state=state)
    log.info("Google devolveu autorização; trocando o código por token com o verificador salvo")
    try:
        flow.fetch_token(code=code, code_verifier=verifier)
    except Exception as exc:
        detail = getattr(exc, "description", None) or type(exc).__name__
        log.error("troca do código Google falhou: %s", detail)
        if "verifier" in str(detail).lower():
            raise DriveError("Login Google perdeu o verificador. Clique em conectar de novo.") from exc
        raise DriveError("O Google recusou o código. Clique em conectar de novo.") from exc
    finally:
        _pkce_path().unlink(missing_ok=True)
    settings.google_token_path.write_text(flow.credentials.to_json(), encoding="utf-8")
    _account_cache["mtime"] = 0.0
    log.info("token Google salvo em %s", settings.google_token_path.name)
    google_account()


def _service(*, log_missing: bool = True):
    creds = credentials()
    if not creds:
        if log_missing:
            log.warning("operação no Drive cancelada: conta não conectada")
        raise DriveError("Google Drive não conectado")
    path = settings.google_token_path
    mtime = path.stat().st_mtime if path.exists() else 0.0
    cached = _service_cache.get("service")
    if cached is not None and _service_cache.get("mtime") == mtime:
        return cached
    service = build("drive", "v3", credentials=creds, cache_discovery=True)
    _service_cache.update(mtime=mtime, service=service)
    return service


def _fold_month(name: str) -> str:
    folded = unicodedata.normalize("NFD", name.strip().casefold())
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")


def parse_lecture_date(drive_path: str) -> tuple[date | None, str]:
    """Data a partir de ANO/NroMes-NomeMes/Dia-NroMes. Mês divergente usa Dia-NroMes."""
    match = PATH_RE.search(drive_path.replace("\\", "/"))
    if not match:
        return None, ""
    year_s, month_folder_s, month_name, day_s, month_day_s = match.groups()
    year, month_folder, day, month_day = int(year_s), int(month_folder_s), int(day_s), int(month_day_s)
    warnings: list[str] = []
    expected = MONTH_NAMES.get(month_folder)
    if expected and _fold_month(month_name) != _fold_month(expected):
        warnings.append(
            f"nome do mês divergente em {drive_path}: pasta {month_folder_s}-{month_name}, esperado {expected}"
        )
    try:
        parsed = date(year, month_day, day)
    except ValueError:
        try:
            fallback = date(year, month_folder, day)
        except ValueError:
            return None, f"data inválida no caminho {drive_path}"
        warnings.append(f"Dia-NroMes inválido em {drive_path}; usando mês da pasta {month_folder_s}")
        return fallback, "; ".join(warnings)
    if month_day != month_folder:
        warnings.append(
            f"mês divergente em {drive_path}: pasta {month_folder_s}-{month_name}, "
            f"Dia-NroMes {day_s}-{month_day_s}; usando {parsed.isoformat()}"
        )
    return parsed, "; ".join(warnings)


def ensure_free_space(needed_bytes: int) -> None:
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(settings.work_dir).free
    required = max(int(needed_bytes or 0), 0) + DISK_MARGIN_BYTES
    if free < required:
        raise DriveError(
            f"espaço insuficiente em {settings.work_dir}: livre {free} bytes, necessário {required} bytes"
        )


def _descend(parent_path: str, name: str) -> bool:
    """Só abre ANO / NroMes-NomeMes / Dia-NroMes. O resto não entra na busca."""
    depth = 0 if not parent_path else parent_path.count("/") + 1
    if depth == 0:
        return bool(YEAR_RE.fullmatch(name))
    if depth == 1:
        return bool(MONTH_RE.match(name))
    if depth == 2:
        return bool(DAY_RE.fullmatch(name))
    return False


def change_token() -> str:
    """Token de agora, antes da varredura, para a próxima checagem não perder alteração."""
    service = _service()
    result = _execute(
        "token de alterações do Drive",
        service.changes().getStartPageToken(fields="startPageToken"),
    )
    return str(result.get("startPageToken") or "")


def commit_listing(token: str) -> None:
    if not token:
        return
    set_drive_index(
        {
            "token": token,
            "folder_id": get_folder_id(),
            "folders": sorted(_listed_folders),
        }
    )
    log.info("checkpoint do Drive salvo; a próxima execução não busca se nada mudar")


def should_list() -> bool:
    """Uma chamada de alterações, não uma varredura. Sem checkpoint, a busca completa é necessária."""
    folder = get_folder_id()
    index = get_drive_index()
    token = str(index.get("token") or "")
    known = {str(item) for item in (index.get("folders") or [])}
    if not token or index.get("folder_id") != folder or folder not in known:
        log.info("busca no Drive necessária: sem checkpoint ou a pasta configurada mudou")
        return True
    try:
        changed, new_token = _relevant_changes(token, known, folder)
    except DriveError as exc:
        log.warning("checagem de alterações falhou (%s); a busca completa será feita", exc)
        return True
    if changed:
        log.info("Drive teve alteração na pasta configurada; busca completa necessária")
        return True
    if new_token and new_token != token:
        index["token"] = new_token
        set_drive_index(index)
    log.info("Drive sem alteração na pasta configurada")
    return False


def _relevant_changes(token: str, known: set[str], root: str) -> tuple[bool, str]:
    service = _service()
    page = token
    new_token = token
    ignored = 0
    while page:
        result = _execute(
            "checagem de alterações do Drive",
            service.changes().list(
                pageToken=page,
                pageSize=100,
                spaces="drive",
                includeRemoved=True,
                fields="nextPageToken,newStartPageToken,changes(fileId,removed,file(parents))",
            ),
        )
        for change in result.get("changes") or []:
            file_id = str(change.get("fileId") or "")
            parents = {str(item) for item in ((change.get("file") or {}).get("parents") or [])}
            if file_id == root or file_id in known or root in parents or parents & known:
                log.info(
                    "alteração relevante no Drive: arquivo=%s removido=%s",
                    file_id,
                    bool(change.get("removed")),
                )
                return True, str(result.get("newStartPageToken") or new_token)
            ignored += 1
        page = result.get("nextPageToken")
        if result.get("newStartPageToken"):
            new_token = str(result["newStartPageToken"])
    if ignored:
        log.info("Drive mudou fora da pasta configurada (%s alterações ignoradas)", ignored)
    return False, new_token


def list_videos() -> list[dict]:
    global _listed_folders
    root_id = get_folder_id()
    if not root_id:
        raise DriveError("GOOGLE_DRIVE_FOLDER_ID não configurado")
    service = _service()
    account = google_account() or "conta não identificada"
    log.info("listando Drive: pasta=%s conta=%s", root_id, account)
    videos: list[dict] = []
    too_small = 0
    skipped_folders = 0
    folders = {root_id: ""}
    names: dict[str, set[str]] = {}
    queue = [root_id]
    while queue:
        folder_id = queue.pop()
        page_token = None
        found_here: list[dict] = []
        while True:
            result = _execute(
                f"listagem da pasta {folders.get(folder_id) or folder_id}",
                service.files().list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, parents, size, md5Checksum)",
                    pageSize=200,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ),
            )
            for item in result.get("files", []):
                names.setdefault(folder_id, set()).add(item["name"])
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    if not _descend(folders[folder_id], item["name"]):
                        skipped_folders += 1
                        continue
                    rel = "/".join(p for p in (folders[folder_id], item["name"]) if p)
                    folders[item["id"]] = rel
                    queue.append(item["id"])
                elif item["name"].lower().endswith(".mp4"):
                    if int(item.get("size") or 0) < settings.min_video_bytes:
                        too_small += 1
                        continue
                    rel = folders[folder_id]
                    pattern_ok = bool(PATH_RE.search(rel))
                    lecture_date, date_warning = parse_lecture_date(rel) if pattern_ok else (None, "")
                    if not pattern_ok:
                        date_warning = f"pasta fora do padrão {PATH_LABEL}: {rel or '/'}"
                    found_here.append(
                        {
                            "id": item["id"],
                            "name": item["name"],
                            "folder_id": folder_id,
                            "path": rel,
                            "size": int(item.get("size") or 0),
                            "md5": item.get("md5Checksum") or "",
                            "pattern_ok": pattern_ok,
                            "lecture_date": lecture_date,
                            "date_warning": date_warning,
                        }
                    )
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        present = names.get(folder_id, set())
        for video in found_here:
            stem = Path(video["name"]).stem
            video["has_outputs"] = f"{stem}.txt" in present and f"{stem}-resumo.pdf" in present
        videos.extend(found_here)
    _listed_folders = set(folders)
    log.info(
        "Drive listou %s mp4 em %s pastas (%s ignorados por serem menores que %s bytes, %s pastas fora do padrão não abertas)",
        len(videos),
        len(folders),
        too_small,
        settings.min_video_bytes,
        skipped_folders,
    )
    return videos


def file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(
    file_id: str,
    dest: Path,
    expected_size: int = 0,
    progress: Callable[[float], None] | None = None,
    cancel: Event | None = None,
    expected_md5: str = "",
) -> None:
    service = _service()
    request = service.files().get_media(fileId=file_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info(
        "baixando %s (id=%s, esperado=%s bytes) como %s",
        dest.name,
        file_id,
        expected_size or "desconhecido",
        google_account() or "conta não identificada",
    )
    last_step = -1
    with dest.open("wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            if cancel is not None and cancel.is_set():
                fh.close()
                dest.unlink(missing_ok=True)
                log.warning("download de %s cancelado", dest.name)
                raise DriveCancelled("Download cancelado")
            try:
                status, done = downloader.next_chunk(num_retries=5)
            except HttpError as exc:
                detail = _http_error(exc)
                log.error("download de %s interrompido: %s", dest.name, detail)
                status_code = getattr(exc.resp, "status", 0)
                error_cls = DriveTransient if status_code in {408, 429} or status_code >= 500 else DriveError
                raise error_cls(f"Google recusou download de {dest.name}: {detail}") from exc
            except (OSError, TimeoutError) as exc:
                if isinstance(exc, DriveError) or isinstance(exc, FileNotFoundError | PermissionError):
                    raise
                log.error("download de %s interrompido por falha de rede: %s", dest.name, exc)
                raise DriveTransient(f"Falha de rede ao baixar {dest.name}: {exc}") from exc
            if status:
                if progress:
                    progress(status.progress())
                step = int(status.progress() * 10)
                if step != last_step:
                    last_step = step
                    log.info("download %s: %s%%", dest.name, step * 10)
    size = dest.stat().st_size
    if expected_size and size < expected_size * 0.98:
        dest.unlink(missing_ok=True)
        log.error("download incompleto de %s: %s de %s bytes", dest.name, size, expected_size)
        raise DriveIncomplete("Download incompleto")
    if expected_md5:
        got = file_md5(dest)
        if got.lower() != expected_md5.lower():
            dest.unlink(missing_ok=True)
            log.error("md5 de %s não confere: %s != %s", dest.name, got, expected_md5)
            raise DriveIncomplete("Checksum MD5 não confere")
        log.info("download conferido por md5: %s", dest.name)
    log.info("download concluído: %s (%s bytes)", dest.name, size)


def download_named(folder_id: str, name: str, dest: Path) -> bool:
    service = _service()
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    found = _execute(
        f"busca de {name}",
        service.files().list(
            q=f"name = '{safe}' and '{folder_id}' in parents and trashed = false",
            fields="files(id, size)",
        ),
    ).get("files", [])
    if not found:
        log.info("arquivo %s não está na pasta %s", name, folder_id)
        return False
    download_file(found[0]["id"], dest, expected_size=int(found[0].get("size") or 0))
    return True


def upload_file(local: Path, folder_id: str, mime: str) -> str:
    service = _service()
    existing = _execute(
        f"busca para envio de {local.name}",
        service.files().list(
            q=f"name = '{local.name}' and '{folder_id}' in parents and trashed = false",
            fields="files(id)",
        ),
    ).get("files", [])
    media = MediaFileUpload(str(local), mimetype=mime, resumable=True)
    account = google_account() or "conta não identificada"
    if existing:
        log.info("atualizando %s na pasta %s como %s", local.name, folder_id, account)
        updated = _execute(
            f"atualização de {local.name}",
            service.files().update(fileId=existing[0]["id"], media_body=media),
        )
        return updated["id"]
    log.info("enviando %s para a pasta %s como %s", local.name, folder_id, account)
    created = _execute(
        f"envio de {local.name}",
        service.files().create(body={"name": local.name, "parents": [folder_id]}, media_body=media),
    )
    return created["id"]


def folder_has_outputs(folder_id: str, stem: str) -> bool:
    service = _service()
    names = {f"{stem}.txt", f"{stem}-resumo.pdf"}
    result = _execute(
        f"checagem de saídas em {folder_id}",
        service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="files(name)",
        ),
    )
    found = {f["name"] for f in result.get("files", [])}
    return names.issubset(found)
