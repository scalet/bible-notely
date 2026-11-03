from __future__ import annotations

import argparse
import asyncio
import shutil
from pathlib import Path

from sqlalchemy import func

from backend.app.config import settings
from backend.app.db import SessionLocal, ensure_schema
from backend.app.logging_setup import setup_logging
from backend.app.models import Lecture
from backend.app.runtime import get_drive_video_count, pipeline_snapshot
from backend.app.services.pipeline import recover_if_idle, reset_incomplete_work, run_batch
from backend.app.services.speakers import import_fotos


def print_status(db) -> None:
    rows = db.query(Lecture.status, func.count(Lecture.id)).group_by(Lecture.status).all()
    compressed = db.query(func.count(Lecture.id)).filter(Lecture.compressed.is_(True)).scalar() or 0
    print("aulas:")
    if not rows:
        print("  (nenhuma)")
    for status, count in rows:
        print(f"  {status}: {count}")
    print(f"comprimidos: {compressed}")
    failed = (
        db.query(Lecture)
        .filter(Lecture.status == "failed")
        .order_by(Lecture.updated_at.desc())
        .limit(8)
        .all()
    )
    for row in failed:
        print(f"  falha #{row.id} {row.video_filename}: {row.error}")
    usage = shutil.disk_usage(settings.work_dir)
    print(f"work livre: {usage.free // (1024 * 1024)} MiB")
    drive_count = get_drive_video_count()
    print(f"mp4 no Drive (último sync): {drive_count if drive_count is not None else 'desconhecido'}")
    snap = pipeline_snapshot()
    print(f"pipeline: {snap.get('message') or 'ocioso'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="bible-notely pipeline")
    parser.add_argument(
        "command",
        choices=[
            "run",
            "status",
            "speakers",
            "obsidian",
            "cleanup",
            "youtube-auth",
            "youtube-catalog",
            "youtube-upload",
            "youtube-sync",
            "compress",
            "overlay",
            "hymnal",
        ],
        nargs="?",
        default="run",
    )
    parser.add_argument("--max", type=int, default=1, help="Vídeos por execução (run / youtube-upload / compress)")
    parser.add_argument("--id", type=int, default=0, help="Aula específica (compress / overlay)")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Baixa o MP4 do Drive se a cópia local não existir (compress / overlay)",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Não espera resposta de letra; o vídeo segue com o que já foi encontrado (overlay)",
    )
    parser.add_argument("--path", default="", help="Zip ou pasta do Cantor Cristão (hymnal)")
    parser.add_argument(
        "--drive",
        action="store_true",
        help="Baixa Cantor_Cristão.zip da pasta Igreja no Drive (hymnal)",
    )
    args = parser.parse_args()
    setup_logging()
    ensure_schema()
    db = SessionLocal()
    try:
        if args.command == "status":
            recovered = recover_if_idle(db)
            if recovered:
                print(f"recuperadas da interrupção: {recovered}")
            print_status(db)
            return
        if args.command == "obsidian":
            from backend.app.services.obsidian import export_all

            done, skipped = export_all(db)
            print(f"pasta={settings.obsidian_dir} exportados={done} sem resumo={skipped}")
            return
        if args.command == "cleanup":
            if pipeline_snapshot().get("running"):
                print("pipeline em execução; pause ou cancele antes de limpar")
                return
            removed = reset_incomplete_work(db)
            if not removed:
                print("nenhum MP4 incompleto em work/")
            else:
                print(f"removidos={len(removed)}")
                for path in removed:
                    print(f"  {path}")
            print_status(db)
            return
        if args.command == "speakers":
            result = import_fotos(db)
            print(
                f"fotos={result['directory']} criados={result['created']} "
                f"atualizados={result['updated']} ignorados={result['skipped']}"
            )
            for error in result["errors"]:
                print(f"  erro: {error}")
            return
        if args.command == "youtube-auth":
            from backend.app.services.youtube import YouTubeError, youtube_auth_local

            try:
                path = youtube_auth_local()
            except YouTubeError as exc:
                print(f"erro: {exc}")
                return
            print(f"YouTube conectado; token em {path}")
            return
        if args.command == "youtube-catalog":
            from backend.app.services.youtube import YouTubeError, export_catalog

            try:
                count, folder_id = export_catalog(db)
            except YouTubeError as exc:
                print(f"erro: {exc}")
                return
            print(f"catalog.json enviado: itens={count} pasta_drive={folder_id}")
            return
        if args.command == "youtube-upload":
            from backend.app.services.youtube import YouTubeError, upload_lectures_local

            try:
                sent, skipped, failed = upload_lectures_local(db, max_items=max(1, args.max))
            except YouTubeError as exc:
                print(f"erro: {exc}")
                return
            print(f"youtube-upload: enviados={sent} pulados={skipped} falhas={failed}")
            return
        if args.command == "youtube-sync":
            from backend.app.services.youtube import YouTubeError, sync_all

            try:
                updated, missing, total = sync_all(db)
            except YouTubeError as exc:
                print(f"erro: {exc}")
                return
            print(f"youtube-sync: atualizadas={updated} sem_aula={missing} entradas_state={total}")
            return
        if args.command == "hymnal":
            from backend.app.services.hymnal import HymnalError, hymnal_dir, import_hymnal, import_hymnal_from_drive

            try:
                if args.drive:
                    count, dest = import_hymnal_from_drive()
                elif args.path:
                    count = import_hymnal(Path(args.path))
                    dest = hymnal_dir()
                else:
                    print("informe --path ou --drive")
                    return
            except HymnalError as exc:
                print(f"erro: {exc}")
                return
            print(f"hinário indexado: {count} hinos em {dest}")
            return
        if args.command == "overlay":
            from backend.app.services.overlays import OverlayCancelled, OverlayError, run_lecture_overlay
            from backend.app.services.pipeline import PipelineBusy, pipeline_lock

            if not args.id:
                print("informe --id da aula")
                return
            lecture = db.get(Lecture, args.id)
            if lecture is None:
                print(f"erro: aula {args.id} não existe")
                return
            if pipeline_snapshot().get("running"):
                print("pipeline em execução; pause ou cancele antes do overlay")
                return
            try:
                message = run_lecture_overlay(
                    db,
                    lecture,
                    no_wait=args.no_wait,
                    fetch=args.fetch,
                    lock=pipeline_lock,
                )
            except PipelineBusy:
                print("pipeline em execução; pause ou cancele antes do overlay")
                return
            except OverlayCancelled:
                print("overlay cancelado")
                return
            except OverlayError as exc:
                print(f"erro: {exc}")
                return
            print(f"overlay: {message}")
            return
        if args.command == "compress":
            from backend.app.services.compress import CompressCancelled, CompressError, compress_backfill
            from backend.app.services.pipeline import PipelineBusy, pipeline_lock, recover_interrupted

            if pipeline_snapshot().get("running"):
                print("pipeline em execução; pause ou cancele antes de comprimir")
                return
            try:
                with pipeline_lock():
                    recover_interrupted(db)
                    done, skipped, missing, failed = compress_backfill(
                        db,
                        lecture_id=args.id or None,
                        max_items=max(1, args.max),
                        fetch=args.fetch,
                    )
            except PipelineBusy:
                print("pipeline em execução; pause ou cancele antes de comprimir")
                return
            except CompressCancelled:
                print("compressão cancelada")
                return
            except CompressError as exc:
                print(f"erro: {exc}")
                return
            print(f"compress: comprimidos={done} pulados={skipped} sem_arquivo={missing} falhas={failed}")
            return
        processed = asyncio.run(run_batch(db, max_items=args.max))
        print(f"processados={processed}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
