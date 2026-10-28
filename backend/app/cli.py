from __future__ import annotations

import argparse
import asyncio
import shutil

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
    print("aulas:")
    if not rows:
        print("  (nenhuma)")
    for status, count in rows:
        print(f"  {status}: {count}")
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
        choices=["run", "status", "speakers", "obsidian", "cleanup"],
        nargs="?",
        default="run",
    )
    parser.add_argument("--max", type=int, default=1, help="Vídeos por execução (um por vez)")
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
        processed = asyncio.run(run_batch(db, max_items=args.max))
        print(f"processados={processed}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
