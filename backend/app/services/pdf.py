from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import Image, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from backend.app.config import settings
from backend.app.models import Lecture, Speaker
from backend.app.services.bible import attach_scripture, format_prompt_block, load_passage
from backend.app.services.llm import generate_json
from backend.app.services.obsidian import parse_reference, reference_spans
from backend.app.services.transcribe import strip_timestamps

# Corte só para aulas muito longas; cortar cedo apaga o fim e o encerramento.
TRANSCRIPT_LIMIT = 250_000
ACCENT = "#7a5c1e"
VERSE_BG = "#f7f3ea"


def _styles():
    base = getSampleStyleSheet()
    return {
        "kicker": ParagraphStyle(
            "kicker",
            parent=base["Normal"],
            textColor=colors.HexColor("#8a8a8a"),
            fontSize=9,
            letterSpacing=1.2,
            spaceAfter=4,
        ),
        "title": ParagraphStyle(
            "title",
            parent=base["Heading1"],
            fontSize=20,
            textColor=colors.HexColor("#111111"),
            spaceAfter=8,
            leading=24,
        ),
        "meta": ParagraphStyle(
            "meta",
            parent=base["Normal"],
            textColor=colors.HexColor("#555555"),
            fontSize=10,
            spaceAfter=4,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontSize=12,
            textColor=colors.HexColor("#111111"),
            spaceBefore=12,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "body",
            parent=base["Normal"],
            fontSize=10.5,
            leading=15,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#222222"),
        ),
        "ref": ParagraphStyle(
            "ref",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13,
            textColor=colors.HexColor(ACCENT),
        ),
        "verse": ParagraphStyle(
            "verse",
            parent=base["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#2b2b2b"),
        ),
    }


async def build_summary(lecture: Lecture, speaker: Speaker | None, transcript: str) -> dict:
    transcript = strip_timestamps(transcript)
    prompt_template = settings.prompt_path.read_text(encoding="utf-8")
    speaker_name = speaker.name if speaker else "Não identificado"
    date_label = lecture.lecture_date.strftime("%d/%m/%Y") if lecture.lecture_date else "sem data"
    known = _prefetch_scripture(transcript)
    arc = format_prompt_block(known)
    prompt = (
        f"{prompt_template}\n\n"
        f"Palestrante: {speaker_name}\n"
        f"Data: {date_label}\n\n"
        f"{arc}\n\n"
        f"TRANSCRIÇÃO:\n{transcript[:TRANSCRIPT_LIMIT]}"
    )
    data = await generate_json(prompt, system="Retorne somente JSON válido.")
    data.setdefault("titulo", "Resumo da aula")
    data.setdefault("texto_base", "")
    data.setdefault("pontos", [])
    data.setdefault("aplicacoes", [])
    data.setdefault("conclusao", "")
    data.setdefault("encerramento", "")
    data.setdefault("referencias", [])
    return attach_scripture(data)


def _prefetch_scripture(transcript: str) -> list[dict]:
    """Busca a ARC das referências já escritas na transcrição, para a LLM copiar."""
    passages = []
    seen: set[tuple[str, int, str]] = set()
    for book, chapter, verses in parse_reference(transcript[:TRANSCRIPT_LIMIT]):
        if not verses:
            continue
        key = (book, chapter, verses)
        if key in seen:
            continue
        seen.add(key)
        passage = load_passage(book, chapter, verses)
        if passage:
            passages.append(passage)
        if len(passages) >= 8:
            break
    return passages


def _xml(value: object) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _rich(value: object) -> str:
    """Escapa o texto e realça as referências bíblicas (negrito na cor de destaque)."""
    text = str(value or "")
    out: list[str] = []
    last = 0
    for start, end in reference_spans(text):
        if start < last:
            continue
        out.append(_xml(text[last:start]))
        out.append(f'<font color="{ACCENT}"><b>{_xml(text[start:end])}</b></font>')
        last = end
    out.append(_xml(text[last:]))
    return "".join(out)


def _canvas_factory(footer_left: str, header_text: str):
    """Canvas de duas passadas: cabeçalho corrido a partir da página 2 e rodapé 'Página X de Y'."""

    class _Canvas(pdfcanvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved: list[dict] = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self._decorate(total)
                super().showPage()
            super().save()

        def _decorate(self, total: int) -> None:
            width, height = A4
            self.setStrokeColor(colors.HexColor("#d8d2c4"))
            self.setLineWidth(0.5)
            self.setFillColor(colors.HexColor("#8a8a8a"))
            self.setFont("Helvetica", 8)
            if self._pageNumber > 1:
                self.drawString(2 * cm, height - 1.0 * cm, header_text)
                self.line(2 * cm, height - 1.2 * cm, width - 2 * cm, height - 1.2 * cm)
            self.line(2 * cm, 1.3 * cm, width - 2 * cm, 1.3 * cm)
            self.drawString(2 * cm, 0.85 * cm, footer_left)
            self.drawRightString(width - 2 * cm, 0.85 * cm, f"Página {self._pageNumber} de {total}")

    return _Canvas


def _portrait(speaker: Speaker | None) -> Path | None:
    if speaker is None:
        return None
    fotos = settings.speakers_fotos_dir
    if not fotos.exists():
        return None
    for path in sorted(fotos.iterdir()):
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        stem = path.stem.split("|", 1)[0].replace("_", " ").strip()
        if stem.casefold() == speaker.name.casefold():
            return path
    return None


def _speaker_photo(speaker: Speaker | None) -> Path | None:
    """O modelo usa o recorte quadrado; o retrato de fotos/ só entra se não houver recorte."""
    if speaker is None:
        return None
    crop = Path(speaker.photo_path) if speaker.photo_path else None
    if crop and crop.exists():
        return crop
    return _portrait(speaker)


def _fitted_photo(path: Path, box: float) -> Image:
    width, height = ImageReader(str(path)).getSize()
    scale = min(box / width, box / height)
    return Image(str(path), width=width * scale, height=height * scale)


def render_pdf(
    dest: Path,
    lecture: Lecture,
    speaker: Speaker | None,
    summary: dict,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    styles = _styles()
    story: list = []

    header_cells: list = []
    photo = _speaker_photo(speaker)
    if photo and _is_crop(speaker, photo):
        header_cells.append(Image(str(photo), width=2.4 * cm, height=2.4 * cm))
    elif photo:
        header_cells.append(_fitted_photo(photo, 2.4 * cm))
    else:
        header_cells.append("")

    name = speaker.name if speaker else "Palestrante não identificado"
    role = (speaker.role or "").strip() if speaker else ""
    who = f"{name} · {role}" if role else name
    date_label = lecture.lecture_date.strftime("%d/%m/%Y") if lecture.lecture_date else "—"
    heading = "Resumo da Aula da Escola Bíblica Dominical" + (f" - {date_label}" if lecture.lecture_date else "")
    title_block = [
        Paragraph("ESCOLA BÍBLICA DOMINICAL", styles["kicker"]),
        Paragraph(_xml(heading), styles["title"]),
        Paragraph(
            _xml("  ·  ".join(part for part in (who, date_label, lecture.drive_path) if part)),
            styles["meta"],
        ),
    ]
    header_cells.append(title_block)
    table = Table([header_cells], colWidths=[2.8 * cm, 14.2 * cm])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 8))

    escrituras = [p for p in (summary.get("escrituras") or []) if p.get("verses")]

    if str(summary.get("texto_base") or "").strip():
        story.append(Paragraph("Texto-base", styles["h2"]))
        story.append(Paragraph(_rich(str(summary["texto_base"]).strip()), styles["body"]))
    pontos = summary.get("pontos") or []
    if pontos:
        story.append(Paragraph("Pontos principais", styles["h2"]))
        story.append(
            ListFlowable(
                [ListItem(Paragraph(_rich(p), styles["body"])) for p in pontos],
                bulletType="bullet",
            )
        )

    apps = summary.get("aplicacoes") or []
    if apps:
        story.append(Paragraph("Aplicações", styles["h2"]))
        story.append(
            ListFlowable(
                [ListItem(Paragraph(_rich(p), styles["body"])) for p in apps],
                bulletType="bullet",
            )
        )

    if summary.get("conclusao"):
        story.append(Paragraph("Conclusão", styles["h2"]))
        story.append(Paragraph(_rich(summary["conclusao"]), styles["body"]))

    if str(summary.get("encerramento") or "").strip():
        story.append(Paragraph("Encerramento da EBD", styles["h2"]))
        story.append(Paragraph(_rich(summary["encerramento"]), styles["body"]))

    if escrituras:
        story.append(Paragraph("Passagens citadas (ARC)", styles["h2"]))
        for passage in escrituras:
            story.append(_scripture(passage, styles))
            story.append(Spacer(1, 8))

    doc = SimpleDocTemplate(
        str(dest),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=1.6 * cm,
        bottomMargin=1.8 * cm,
        title=heading,
        author=speaker.name if speaker else "bible-notely",
    )
    footer_left = " · ".join(part for part in (who, date_label if lecture.lecture_date else "") if part)
    doc.build(story, canvasmaker=_canvas_factory(footer_left, heading))


def _is_crop(speaker: Speaker | None, photo: Path) -> bool:
    if speaker is None or not speaker.photo_path:
        return False
    return Path(speaker.photo_path) == photo


def _scripture(passage: dict, styles: dict) -> Table:
    """Bloco destacado: faixa lateral, fundo claro, referência em destaque e versículos em itálico."""
    rows = [[Paragraph(_xml(passage.get("label") or ""), styles["ref"])]]
    for item in passage.get("items") or []:
        rows.append([Paragraph(f"<super>{item['n']}</super> {_xml(item['text'])}", styles["verse"])])
    if passage.get("truncated"):
        rows.append([Paragraph("…", styles["verse"])])
    block = Table(rows, colWidths=[17 * cm], splitByRow=1)
    block.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(VERSE_BG)),
                ("LINEBEFORE", (0, 0), (0, -1), 2.5, colors.HexColor(ACCENT)),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, 0), 6),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
            ]
        )
    )
    return block


def summary_plaintext(summary: dict) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2)

