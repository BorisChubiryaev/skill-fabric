#!/usr/bin/env python3
"""Конвертация PDF в редактируемую PPTX-презентацию.

Инструмент навыка pdf-to-pptx. Зависит только от PyMuPDF (pymupdf) и
python-pptx. Не выполняет OCR и не обращается к сети.

Команды:
  analyze  — разведка PDF: страницы, размеры, текст/скан, картинки, шрифты,
             предупреждения. Ничего не создаёт.
  convert  — сборка PPTX и Markdown-отчёта в трёх режимах: text | hybrid | image.
  verify   — независимая проверка готового PPTX (слайды, фигуры, текст).

Философия: не выдавать частичный результат за готовый. Каждая команда
возвращает JSON в stdout; ненулевой код выхода означает, что результат не готов.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24 экспортирует модуль pymupdf
except Exception:  # pragma: no cover - совместимость со старым именем
    import fitz  # type: ignore

from pptx import Presentation
from pptx.util import Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_AUTO_SIZE, PP_ALIGN
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.oxml.ns import qn


# --- Единицы: PDF в пунктах (1/72"), python-pptx понимает Pt напрямую. -------
# Растр рендерим в увеличении, но кладём его в координатах страницы в пунктах,
# поэтому масштаб DPI не влияет на геометрию — только на чёткость подложки.


# Флаги начертания PyMuPDF (span["flags"]).
_FLAG_ITALIC = 1 << 1
_FLAG_BOLD = 1 << 4


def _emit(payload: dict[str, Any], ok: bool) -> int:
    payload = {"ok": bool(ok), **payload}
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if ok else 1


def _int_color_to_rgb(value: int | None) -> RGBColor | None:
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return RGBColor((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)


def _open_pdf(path: str) -> "fitz.Document":
    doc = fitz.open(path)
    if doc.is_encrypted:
        # Пустой пароль иногда снимает шифрование "владельца".
        if not doc.authenticate(""):
            doc.close()
            raise ValueError("PDF зашифрован и требует пароль")
    return doc


# --- Модель извлечения --------------------------------------------------------


@dataclass
class SpanRun:
    text: str
    size: float
    color: int | None
    bold: bool
    italic: bool
    font: str


@dataclass
class LineBox:
    x0: float
    y0: float
    x1: float
    y1: float
    runs: list[SpanRun] = field(default_factory=list)


@dataclass
class ImageBox:
    x0: float
    y0: float
    x1: float
    y1: float
    data: bytes
    ext: str


@dataclass
class PageModel:
    index: int
    width: float
    height: float
    rotation: int
    lines: list[LineBox]
    images: list[ImageBox]
    has_vector: bool
    warnings: list[str]

    @property
    def char_count(self) -> int:
        return sum(len(r.text) for ln in self.lines for r in ln.runs)


def _extract_page(page: "fitz.Page", idx: int) -> PageModel:
    warnings: list[str] = []
    rect = page.rect
    rotation = int(page.rotation or 0)
    if rotation:
        warnings.append(
            f"страница повёрнута на {rotation}°: позиции текста могут быть неточны"
        )

    lines: list[LineBox] = []
    images: list[ImageBox] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        btype = block.get("type", 0)
        if btype == 1:  # растровое изображение
            bbox = block.get("bbox")
            img = block.get("image")
            if bbox and img:
                images.append(
                    ImageBox(
                        x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3],
                        data=img, ext=block.get("ext", "png"),
                    )
                )
            continue
        for line in block.get("lines", []):
            runs: list[SpanRun] = []
            xs0 = ys0 = 1e18
            xs1 = ys1 = -1e18
            for span in line.get("spans", []):
                text = span.get("text", "")
                if text == "":
                    continue
                bbox = span.get("bbox", line.get("bbox"))
                xs0 = min(xs0, bbox[0]); ys0 = min(ys0, bbox[1])
                xs1 = max(xs1, bbox[2]); ys1 = max(ys1, bbox[3])
                flags = int(span.get("flags", 0))
                font = str(span.get("font", ""))
                fl = font.lower()
                bold = bool(flags & _FLAG_BOLD) or "bold" in fl or "black" in fl or "heavy" in fl
                italic = bool(flags & _FLAG_ITALIC) or "italic" in fl or "oblique" in fl
                runs.append(
                    SpanRun(
                        text=text,
                        size=float(span.get("size", 12.0)),
                        color=span.get("color"),
                        bold=bold,
                        italic=italic,
                        font=font,
                    )
                )
            if runs:
                lines.append(LineBox(xs0, ys0, xs1, ys1, runs))

    has_vector = False
    try:
        drawings = page.get_drawings()
        has_vector = len(drawings) > 0
    except Exception:
        has_vector = False

    return PageModel(
        index=idx,
        width=float(rect.width),
        height=float(rect.height),
        rotation=rotation,
        lines=lines,
        images=images,
        has_vector=has_vector,
        warnings=warnings,
    )


# --- analyze ------------------------------------------------------------------


def cmd_analyze(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.input):
        return _emit({"error": f"файл не найден: {args.input}"}, ok=False)
    try:
        doc = _open_pdf(args.input)
    except Exception as exc:  # noqa: BLE001
        return _emit({"error": f"не удалось открыть PDF: {exc}"}, ok=False)

    pages_info = []
    fonts: set[str] = set()
    total_chars = 0
    total_images = 0
    scanned_pages = 0
    with doc:
        for i in range(doc.page_count):
            pm = _extract_page(doc[i], i)
            for ln in pm.lines:
                for r in ln.runs:
                    if r.font:
                        fonts.add(r.font)
            total_chars += pm.char_count
            total_images += len(pm.images)
            is_scanned = pm.char_count == 0 and (len(pm.images) > 0 or pm.has_vector)
            if is_scanned:
                scanned_pages += 1
            pages_info.append({
                "index": i,
                "width_pt": round(pm.width, 1),
                "height_pt": round(pm.height, 1),
                "rotation": pm.rotation,
                "chars": pm.char_count,
                "lines": len(pm.lines),
                "images": len(pm.images),
                "has_vector": pm.has_vector,
                "likely_scanned": is_scanned,
                "warnings": pm.warnings,
            })

    recommendations = []
    if scanned_pages == len(pages_info) and pages_info:
        recommendations.append(
            "весь документ без текстового слоя (скан): извлекаемого текста нет; "
            "режимы text/hybrid дадут пустой текст. Нужен предварительный OCR "
            "(вне этого навыка) или режим image."
        )
    elif scanned_pages:
        recommendations.append(
            f"{scanned_pages} стр. без текстового слоя: на них текст не будет "
            "редактируемым (только подложка)."
        )
    mode = "image" if scanned_pages == len(pages_info) and pages_info else "hybrid"
    recommendations.append(f"рекомендуемый режим: {mode}")

    return _emit({
        "input": os.path.abspath(args.input),
        "page_count": len(pages_info),
        "total_chars": total_chars,
        "total_images": total_images,
        "scanned_pages": scanned_pages,
        "fonts": sorted(fonts),
        "pages": pages_info,
        "recommended_mode": mode,
        "recommendations": recommendations,
    }, ok=True)


# --- Построение PPTX ----------------------------------------------------------


def _blank_layout(prs: Presentation):
    # Ищем пустой макет; если нет — берём последний (обычно "Blank").
    for layout in prs.slide_layouts:
        if layout.name and "blank" in layout.name.lower():
            return layout
    return prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1]


def _render_background_png(page: "fitz.Page", dpi: int, drop_text: bool) -> bytes:
    """Растровая подложка страницы. При drop_text=True текст убирается
    редактированием, векторная графика и картинки сохраняются."""
    if drop_text:
        # Работаем на копии страницы, чтобы не портить исходный документ.
        src = page.parent
        tmp = fitz.open()
        tmp.insert_pdf(src, from_page=page.number, to_page=page.number)
        p2 = tmp[0]
        td = p2.get_text("dict")
        removed = 0
        for block in td.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("text", "").strip() == "":
                        continue
                    p2.add_redact_annot(fitz.Rect(span["bbox"]))
                    removed += 1
        if removed:
            # graphics=... не трогаем: убираем только текст под аннотациями.
            p2.apply_redactions()
        pix = p2.get_pixmap(dpi=dpi)
        data = pix.tobytes("png")
        tmp.close()
        return data
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes("png")


def _add_line_textbox(slide, ln: LineBox) -> None:
    width = max(ln.x1 - ln.x0, 1.0)
    height = max(ln.y1 - ln.y0, 1.0)
    box = slide.shapes.add_textbox(Pt(ln.x0), Pt(ln.y0), Pt(width), Pt(height))
    tf = box.text_frame
    tf.word_wrap = False
    try:
        tf.auto_size = MSO_AUTO_SIZE.NONE
    except Exception:
        pass
    # Убираем внутренние поля, чтобы текст сел на место как в PDF.
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    para = tf.paragraphs[0]
    para.alignment = PP_ALIGN.LEFT
    first = True
    for r in ln.runs:
        run = para.add_run()
        run.text = r.text
        f = run.font
        f.size = Pt(max(r.size, 1.0))
        f.bold = r.bold
        f.italic = r.italic
        if r.font:
            f.name = _clean_font_name(r.font)
        rgb = _int_color_to_rgb(r.color)
        if rgb is not None:
            f.color.rgb = rgb
        first = False
    # Прижать текст к верху блока, чтобы вертикально совпадал с оригиналом.
    try:
        from pptx.enum.text import MSO_ANCHOR
        tf.vertical_anchor = MSO_ANCHOR.TOP
    except Exception:
        pass


def _clean_font_name(font: str) -> str:
    # PDF-шрифты часто вида "ABCDEF+Arial-BoldMT". Приводим к читаемому имени.
    name = font.split("+", 1)[-1]
    for suffix in ("-BoldMT", "-Bold", "-ItalicMT", "-Italic", "MT", "-Regular",
                   "-Oblique", "PSMT", "PS"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.replace("-", " ").strip() or font


def _add_full_slide_picture(slide, prs, png: bytes) -> None:
    stream = io.BytesIO(png)
    slide.shapes.add_picture(stream, 0, 0, width=prs.slide_width, height=prs.slide_height)


def _add_image(slide, im: ImageBox) -> bool:
    try:
        stream = io.BytesIO(im.data)
        slide.shapes.add_picture(
            stream, Pt(im.x0), Pt(im.y0),
            width=Pt(max(im.x1 - im.x0, 1.0)), height=Pt(max(im.y1 - im.y0, 1.0)),
        )
        return True
    except Exception:
        return False


def cmd_convert(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.input):
        return _emit({"error": f"файл не найден: {args.input}"}, ok=False)
    out = args.out
    if os.path.abspath(out) == os.path.abspath(args.input):
        return _emit({"error": "путь результата совпадает с входным"}, ok=False)
    if not out.lower().endswith(".pptx"):
        return _emit({"error": "--out должен оканчиваться на .pptx"}, ok=False)

    mode = args.mode
    dpi = int(args.dpi)

    try:
        doc = _open_pdf(args.input)
    except Exception as exc:  # noqa: BLE001
        return _emit({"error": f"не удалось открыть PDF: {exc}"}, ok=False)

    prs = Presentation()
    layout = _blank_layout(prs)

    stats = {
        "pages": 0, "slides": 0, "text_lines": 0, "text_chars": 0,
        "images_placed": 0, "images_failed": 0, "backgrounds": 0,
    }
    limitations: list[str] = []
    per_page: list[dict[str, Any]] = []

    with doc:
        for i in range(doc.page_count):
            page = doc[i]
            pm = _extract_page(page, i)
            stats["pages"] += 1

            # Размер слайда = размер страницы (в пунктах). PPTX допускает
            # разный размер только один на презентацию, поэтому берём размер
            # первой страницы; остальные подгоняем предупреждением.
            if i == 0:
                prs.slide_width = Pt(pm.width)
                prs.slide_height = Pt(pm.height)
                base_w, base_h = pm.width, pm.height
            elif abs(pm.width - base_w) > 1 or abs(pm.height - base_h) > 1:
                limitation = (
                    f"стр. {i}: размер {round(pm.width)}×{round(pm.height)} pt "
                    f"отличается от первого слайда {round(base_w)}×{round(base_h)} pt; "
                    "содержимое масштабировано под общий размер"
                )
                if limitation not in limitations:
                    limitations.append(
                        "в презентации разные размеры страниц; PPTX хранит один "
                        "размер слайда — часть страниц масштабирована"
                    )

            slide = prs.slides.add_slide(layout)
            stats["slides"] += 1
            page_stat = {"index": i, "mode": mode, "chars": pm.char_count,
                         "lines": len(pm.lines), "images": len(pm.images)}

            # Масштаб для страниц с иным размером (только hybrid/image кладут
            # подложку во весь слайд; текст мы позиционируем в пунктах исходной
            # страницы, что верно лишь при совпадающем размере).
            scaled = (i != 0 and (abs(pm.width - base_w) > 1 or abs(pm.height - base_h) > 1))

            if mode in ("hybrid", "image"):
                drop_text = (mode == "hybrid")
                try:
                    png = _render_background_png(page, dpi=dpi, drop_text=drop_text)
                    _add_full_slide_picture(slide, prs, png)
                    stats["backgrounds"] += 1
                except Exception as exc:  # noqa: BLE001
                    limitations.append(f"стр. {i}: не удалось отрисовать подложку: {exc}")

            if mode in ("text", "hybrid") and not scaled:
                if pm.char_count == 0:
                    page_stat["note"] = "нет текстового слоя (скан?)"
                for ln in pm.lines:
                    _add_line_textbox(slide, ln)
                    stats["text_lines"] += 1
                    stats["text_chars"] += sum(len(r.text) for r in ln.runs)
                if mode == "text":
                    for im in pm.images:
                        if _add_image(slide, im):
                            stats["images_placed"] += 1
                        else:
                            stats["images_failed"] += 1
                    if pm.has_vector:
                        page_stat["note_vector"] = (
                            "есть векторная графика — в режиме text она не переносится; "
                            "используйте hybrid"
                        )
            elif mode in ("text", "hybrid") and scaled:
                page_stat["note"] = (
                    "размер страницы отличается — текст не наложен, оставлена подложка"
                )

            for w in pm.warnings:
                page_stat.setdefault("warnings", []).append(w)
            per_page.append(page_stat)

    # Общие ограничения по режимам.
    if mode == "text":
        limitations.append("режим text: векторная графика и фоны не переносятся; "
                            "визуальная точность приблизительная")
    if mode == "hybrid":
        limitations.append("режим hybrid: графика и картинки — это растровая "
                            "подложка (не редактируется); редактируется только текст")
    if mode == "image":
        limitations.append("режим image: слайды — это картинки, ничего не "
                            "редактируется (максимальная точность)")

    # Публикация через временный файл, чтобы не оставить полурезультат.
    out_dir = os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(out_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pptx", dir=out_dir)
    os.close(tmp_fd)
    try:
        prs.save(tmp_path)
        os.replace(tmp_path, out)
    except Exception as exc:  # noqa: BLE001
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return _emit({"error": f"не удалось сохранить PPTX: {exc}"}, ok=False)

    # Отчёт.
    report_path = args.report
    if report_path:
        _write_report(report_path, args.input, out, mode, dpi, stats,
                      per_page, limitations)

    ok = stats["slides"] > 0
    return _emit({
        "input": os.path.abspath(args.input),
        "output": os.path.abspath(out),
        "report": os.path.abspath(report_path) if report_path else None,
        "mode": mode,
        "dpi": dpi,
        "stats": stats,
        "limitations": limitations,
        "pages": per_page,
    }, ok=ok)


def _write_report(path, inp, out, mode, dpi, stats, per_page, limitations) -> None:
    lines = []
    lines.append(f"# Отчёт конвертации PDF → PPTX\n")
    lines.append(f"- **Вход:** `{os.path.abspath(inp)}`")
    lines.append(f"- **Результат:** `{os.path.abspath(out)}`")
    lines.append(f"- **Режим:** `{mode}`  •  **DPI подложки:** {dpi}\n")
    lines.append("## Сводка\n")
    lines.append(f"- Слайдов: {stats['slides']}")
    lines.append(f"- Текстовых строк: {stats['text_lines']} ({stats['text_chars']} симв.)")
    lines.append(f"- Картинок размещено: {stats['images_placed']} "
                 f"(ошибок: {stats['images_failed']})")
    lines.append(f"- Растровых подложек: {stats['backgrounds']}\n")
    if limitations:
        lines.append("## Ограничения и потери качества\n")
        for lim in dict.fromkeys(limitations):
            lines.append(f"- {lim}")
        lines.append("")
    lines.append("## По страницам\n")
    lines.append("| # | Символов | Строк | Картинок | Примечание |")
    lines.append("|---|---|---|---|---|")
    for p in per_page:
        note = p.get("note", "") or p.get("note_vector", "")
        if p.get("warnings"):
            note = (note + "; " if note else "") + "; ".join(p["warnings"])
        lines.append(f"| {p['index']} | {p.get('chars',0)} | {p.get('lines',0)} "
                     f"| {p.get('images',0)} | {note} |")
    lines.append("")
    lines.append("## Как проверить результат\n")
    lines.append("1. Откройте PPTX в PowerPoint / LibreOffice Impress / Google Slides.")
    lines.append("2. Сверьте вёрстку с исходным PDF постранично.")
    lines.append("3. В режимах text/hybrid текст правится напрямую; в hybrid графика — "
                 "это фоновая картинка.\n")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.replace(tmp, path)


# --- verify -------------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.input):
        return _emit({"error": f"файл не найден: {args.input}"}, ok=False)
    try:
        prs = Presentation(args.input)
    except Exception as exc:  # noqa: BLE001
        return _emit({"error": f"не удалось открыть PPTX: {exc}"}, ok=False)

    slides = list(prs.slides)
    total_text = 0
    total_shapes = 0
    total_pictures = 0
    per_slide = []
    for idx, slide in enumerate(slides):
        chars = 0
        shapes = 0
        pics = 0
        for shape in slide.shapes:
            shapes += 1
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                pics += 1
            if shape.has_text_frame:
                chars += len(shape.text_frame.text)
        total_text += chars
        total_shapes += shapes
        total_pictures += pics
        per_slide.append({"index": idx, "shapes": shapes, "pictures": pics, "chars": chars})

    ok = len(slides) > 0 and total_shapes > 0
    return _emit({
        "input": os.path.abspath(args.input),
        "slide_width_pt": round(Emu(prs.slide_width).pt, 1),
        "slide_height_pt": round(Emu(prs.slide_height).pt, 1),
        "slides": len(slides),
        "total_shapes": total_shapes,
        "total_pictures": total_pictures,
        "total_text_chars": total_text,
        "per_slide": per_slide,
    }, ok=ok)


# --- CLI ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Конвертация PDF в редактируемую PPTX")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze", help="разведка PDF без создания файлов")
    a.add_argument("input")
    a.set_defaults(func=cmd_analyze)

    c = sub.add_parser("convert", help="собрать PPTX и отчёт")
    c.add_argument("input")
    c.add_argument("--out", required=True, help="путь к результату .pptx")
    c.add_argument("--report", default=None, help="путь к Markdown-отчёту")
    c.add_argument("--mode", choices=["text", "hybrid", "image"], default="hybrid")
    c.add_argument("--dpi", type=int, default=150, help="разрешение растровой подложки")
    c.set_defaults(func=cmd_convert)

    v = sub.add_parser("verify", help="проверить готовый PPTX")
    v.add_argument("input")
    v.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
