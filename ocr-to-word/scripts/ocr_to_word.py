#!/usr/bin/env python3
"""Профессиональный офлайн-OCR: изображения / сканы PDF / фото → редактируемый DOCX.

Инструмент навыка ocr-to-word. Работает локально на движке Tesseract, без сети и
API-ключей. Языки по умолчанию: русский + английский (`rus+eng`).

Зависимости:
  - системный бинарь `tesseract` с языковыми пакетами (rus, eng);
  - Python: pytesseract, pillow, numpy, opencv-python(-headless), pymupdf, python-docx.

Команды:
  check  — проверка окружения (движок, языки, пакеты). Ничего не создаёт.
  ocr    — распознать вход и собрать DOCX (+ отчёт, + опционально JSON).

Философия: не выдавать мусор за текст. Инструмент считает уверенность
распознавания по страницам, помечает подозрительные места и честно сообщает,
когда качество входа низкое, вместо тихой «галлюцинации» текста.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any

import numpy as np

try:
    import cv2
except Exception as exc:  # pragma: no cover
    cv2 = None
    _CV2_ERR = exc

try:
    import pytesseract
    from pytesseract import Output
except Exception as exc:  # pragma: no cover
    pytesseract = None
    _PYT_ERR = exc

try:
    import pymupdf as fitz
except Exception:  # pragma: no cover
    try:
        import fitz  # type: ignore
    except Exception:
        fitz = None

from PIL import Image

try:
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_BREAK
except Exception as exc:  # pragma: no cover
    Document = None
    _DOCX_ERR = exc


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}
DEFAULT_LANG = "rus+eng"
# Ниже этого среднего значения уверенности страницу помечаем как ненадёжную.
LOW_CONF_PAGE = 60.0
# Отдельные слова ниже этого порога помечаем как сомнительные в отчёте.
LOW_CONF_WORD = 45.0


def _emit(payload: dict[str, Any], ok: bool) -> int:
    json.dump({"ok": bool(ok), **payload}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if ok else 1


# --- Проверка окружения -------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    problems: list[str] = []
    info: dict[str, Any] = {}

    for name, mod, err_attr in [
        ("pytesseract", pytesseract, "_PYT_ERR"),
        ("opencv", cv2, "_CV2_ERR"),
        ("pymupdf", fitz, None),
        ("python-docx", Document, "_DOCX_ERR"),
    ]:
        if mod is None:
            problems.append(f"не установлен Python-пакет: {name}")

    tver = None
    langs: list[str] = []
    if pytesseract is not None:
        try:
            tver = str(pytesseract.get_tesseract_version())
            langs = list(pytesseract.get_languages(config=""))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"бинарь tesseract недоступен: {exc}")
    info["tesseract_version"] = tver
    info["languages"] = langs

    requested = [l for l in (args.lang or DEFAULT_LANG).split("+") if l]
    missing = [l for l in requested if l not in langs]
    if missing:
        problems.append(
            "нет языковых пакетов Tesseract: " + ", ".join(missing) +
            " (установите, например: apt-get install " +
            " ".join(f"tesseract-ocr-{l}" for l in missing) + ")"
        )

    return _emit({
        "requested_langs": requested,
        "missing_langs": missing,
        "problems": problems,
        **info,
        "ready": not problems,
    }, ok=not problems)


# --- Предобработка изображений ------------------------------------------------


def _to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        if img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def _estimate_skew(gray: np.ndarray) -> float:
    """Оценка угла перекоса строк в градусах. Возвращает угол, НА КОТОРЫЙ НУЖНО
    ПОВЕРНУТЬ изображение для выравнивания (положительный — против часовой).
    0.0, если оценить не удалось или перекос мал.

    Конвенция как в общепринятом рецепте: cv2.minAreaRect даёт угол в [-90, 0),
    корректирующий поворот = -(90+angle) при angle < -45, иначе -angle."""
    thr = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(thr > 0))
    if coords.shape[0] < 50:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    correction = -(90 + angle) if angle < -45 else -angle
    # Углы > 20° — почти всегда ошибка оценки (вертикальный текст, рамки), не перекос.
    if abs(correction) > 20:
        return 0.0
    return float(correction)


def _rotate(img: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.1:
        return img
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    border = 255 if img.ndim == 2 else (255, 255, 255)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def _maybe_upscale(gray: np.ndarray, meta: dict[str, Any]) -> np.ndarray:
    """Tesseract лучше всего работает при высоте текста ~30+ px (≈300 dpi).
    Мелкие изображения увеличиваем, иначе распознавание рассыпается."""
    h, w = gray.shape[:2]
    if max(h, w) < 1000:
        scale = 2
        gray = cv2.resize(gray, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
        meta["steps"].append(f"upscale x{scale}")
    return gray


def preprocess(img: np.ndarray, mode: str) -> tuple[np.ndarray, dict[str, Any]]:
    """Готовит изображение к OCR. Принцип: помогать движку, а не мешать. Сам
    Tesseract отлично бинаризует, поэтому жёсткие пороги по умолчанию не
    применяем — они чаще разрушают текст, чем помогают. Ограничиваемся серым,
    выравниванием перекоса и апскейлом мелких картинок; для фото добавляем
    щадящее подавление шума.

    Режимы:
      none  — только градации серого;
      scan  — серый + выравнивание перекоса + апскейл (чистые сканы);
      photo — то же + щадящее подавление шума и выравнивание контраста (фото);
      auto  — определить перекос и уровень шума и выбрать обработку (по умолчанию).
    Возвращает обработанное изображение (uint8, grayscale) и метаданные."""
    meta: dict[str, Any] = {"mode": mode, "deskew_angle": 0.0, "steps": ["grayscale"]}
    gray = _to_gray(img)

    if mode == "none":
        return gray, meta

    # Выравнивание перекоса — самый надёжный выигрыш для сканов и фото.
    angle = _estimate_skew(gray)
    if abs(angle) >= 0.5:
        gray = _rotate(gray, angle)
        meta["deskew_angle"] = round(angle, 2)
        meta["steps"].append(f"deskew({round(angle, 2)}°)")

    gray = _maybe_upscale(gray, meta)

    # Оценка шума: разброс лапласиана. Высокий на «гладком» тексте → зашумлённое фото.
    noise = float(np.std(cv2.Laplacian(gray, cv2.CV_64F)))
    is_photo = mode == "photo" or (mode == "auto" and noise > 18.0)

    if is_photo:
        # Щадящее подавление шума + локальное выравнивание освещённости (CLAHE).
        gray = cv2.fastNlMeansDenoising(gray, h=7)
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        meta["steps"].extend(["denoise", "clahe"])

    meta["noise_estimate"] = round(noise, 1)
    return gray, meta


# --- Загрузка входа в набор страниц-изображений -------------------------------


@dataclass
class PageImage:
    index: int
    image: np.ndarray  # BGR или GRAY, uint8
    source: str


def _load_pages(path: str, pdf_dpi: int) -> list[PageImage]:
    ext = os.path.splitext(path)[1].lower()
    pages: list[PageImage] = []
    if ext == ".pdf":
        if fitz is None:
            raise RuntimeError("для PDF нужен pymupdf")
        doc = fitz.open(path)
        with doc:
            for i in range(doc.page_count):
                pix = doc[i].get_pixmap(dpi=pdf_dpi)
                arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, pix.n
                )
                if pix.n == 4:
                    arr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
                elif pix.n == 3:
                    arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
                pages.append(PageImage(i, arr.copy(), f"{os.path.basename(path)}#p{i+1}"))
    elif ext in IMAGE_EXTS:
        pil = Image.open(path).convert("RGB")
        arr = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
        pages.append(PageImage(0, arr, os.path.basename(path)))
    else:
        raise ValueError(f"неподдерживаемый тип входа: {ext or '(без расширения)'}")
    return pages


# --- Распознавание ------------------------------------------------------------


@dataclass
class Paragraph:
    text: str
    conf: float
    low_conf_words: list[str] = field(default_factory=list)


@dataclass
class PageResult:
    index: int
    source: str
    paragraphs: list[Paragraph]
    mean_conf: float
    word_count: int
    preprocess: dict[str, Any]

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.paragraphs)


def _ocr_page(img: np.ndarray, lang: str, psm: int) -> tuple[list[Paragraph], float, int, list[dict]]:
    config = f"--oem 3 --psm {psm}"
    data = pytesseract.image_to_data(img, lang=lang, config=config,
                                     output_type=Output.DICT)
    n = len(data["text"])
    # Группируем слова по (block, par) → абзац; строки внутри абзаца сливаем.
    groups: dict[tuple[int, int], dict[str, Any]] = {}
    order: list[tuple[int, int]] = []
    words_json: list[dict] = []
    all_conf: list[float] = []
    word_count = 0
    for i in range(n):
        text = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if text == "" or conf < 0:
            continue
        key = (int(data["block_num"][i]), int(data["par_num"][i]))
        if key not in groups:
            groups[key] = {"lines": {}, "confs": []}
            order.append(key)
        line = int(data["line_num"][i])
        groups[key]["lines"].setdefault(line, []).append((int(data["word_num"][i]), text, conf))
        groups[key]["confs"].append(conf)
        all_conf.append(conf)
        word_count += 1
        words_json.append({
            "text": text, "conf": round(conf, 1),
            "left": int(data["left"][i]), "top": int(data["top"][i]),
            "width": int(data["width"][i]), "height": int(data["height"][i]),
            "block": key[0], "par": key[1], "line": line,
        })

    paragraphs: list[Paragraph] = []
    for key in order:
        g = groups[key]
        parts: list[str] = []
        low: list[str] = []
        for line in sorted(g["lines"]):
            words = [w for w in sorted(g["lines"][line], key=lambda x: x[0])]
            parts.append(" ".join(w[1] for w in words))
            low.extend(w[1] for w in words if w[2] < LOW_CONF_WORD)
        pconf = float(np.mean(g["confs"])) if g["confs"] else 0.0
        # Строки одного абзаца соединяем пробелом — это переносы вёрстки, не абзацы.
        paragraphs.append(Paragraph(text=" ".join(parts).strip(),
                                    conf=round(pconf, 1), low_conf_words=low))
    mean_conf = round(float(np.mean(all_conf)), 1) if all_conf else 0.0
    return paragraphs, mean_conf, word_count, words_json


# --- Сборка DOCX --------------------------------------------------------------


def _build_docx(pages: list[PageResult], out_path: str, title: str,
                flag_low_conf: bool) -> None:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)

    for pi, page in enumerate(pages):
        if pi > 0:
            doc.add_page_break()
        for para in page.paragraphs:
            if not para.text:
                continue
            p = doc.add_paragraph()
            run = p.add_run(para.text)
            # Помечаем ненадёжные абзацы цветом, чтобы человек их перепроверил.
            if flag_low_conf and para.conf < LOW_CONF_PAGE:
                run.font.color.rgb = RGBColor(0xB0, 0x00, 0x00)
        if not any(p.text for p in page.paragraphs):
            doc.add_paragraph("[на этой странице текст не распознан]")

    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=out_dir)
    os.close(fd)
    try:
        doc.save(tmp)
        os.replace(tmp, out_path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _write_report(path: str, inp: str, out: str, lang: str, pages: list[PageResult],
                  overall_conf: float) -> None:
    lines = ["# Отчёт распознавания (OCR → DOCX)\n"]
    lines.append(f"- **Вход:** `{os.path.abspath(inp)}`")
    lines.append(f"- **Результат:** `{os.path.abspath(out)}`")
    lines.append(f"- **Языки:** `{lang}`  •  **Движок:** Tesseract\n")
    total_words = sum(p.word_count for p in pages)
    lines.append("## Сводка\n")
    lines.append(f"- Страниц: {len(pages)}")
    lines.append(f"- Распознано слов: {total_words}")
    lines.append(f"- Средняя уверенность: {overall_conf}%")
    weak = [p for p in pages if p.mean_conf < LOW_CONF_PAGE]
    if weak:
        lines.append(f"- ⚠️ Ненадёжных страниц (уверенность < {int(LOW_CONF_PAGE)}%): "
                     f"{', '.join(str(p.index + 1) for p in weak)} — перепроверьте вручную")
    lines.append("")
    lines.append("## По страницам\n")
    lines.append("| Стр. | Слов | Уверенность | Обработка | Примечание |")
    lines.append("|---|---|---|---|---|")
    for p in pages:
        note = "ok" if p.mean_conf >= LOW_CONF_PAGE else "⚠️ низкая уверенность"
        if p.word_count == 0:
            note = "текст не распознан"
        steps = ", ".join(p.preprocess.get("steps", []))
        lines.append(f"| {p.index + 1} | {p.word_count} | {p.mean_conf}% | {steps} | {note} |")
    lines.append("")
    lines.append("## Как проверить\n")
    lines.append("1. Откройте DOCX и сверьте с оригиналом, начиная с помеченных "
                 "красным (ненадёжных) фрагментов.")
    lines.append("2. Низкая уверенность обычно = плохой скан/фото: увеличьте "
                 "разрешение, уберите блики, снимайте ровно и повторите.\n")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.replace(tmp, path)


# --- Команда ocr --------------------------------------------------------------


def cmd_ocr(args: argparse.Namespace) -> int:
    if pytesseract is None or cv2 is None or Document is None:
        return _emit({"error": "не установлены зависимости; запустите команду check"},
                     ok=False)
    if not os.path.isfile(args.input):
        return _emit({"error": f"файл не найден: {args.input}"}, ok=False)
    if not args.out.lower().endswith(".docx"):
        return _emit({"error": "--out должен оканчиваться на .docx"}, ok=False)
    if os.path.abspath(args.out) == os.path.abspath(args.input):
        return _emit({"error": "путь результата совпадает с входным"}, ok=False)

    lang = args.lang or DEFAULT_LANG
    # Быстрая проверка языков, чтобы не выдать пустой результат из-за отсутствия пакета.
    try:
        have = set(pytesseract.get_languages(config=""))
    except Exception as exc:  # noqa: BLE001
        return _emit({"error": f"tesseract недоступен: {exc}"}, ok=False)
    missing = [l for l in lang.split("+") if l and l not in have]
    if missing:
        return _emit({"error": f"нет языковых пакетов: {', '.join(missing)}"}, ok=False)

    try:
        raw_pages = _load_pages(args.input, args.pdf_dpi)
    except Exception as exc:  # noqa: BLE001
        return _emit({"error": f"не удалось прочитать вход: {exc}"}, ok=False)
    if not raw_pages:
        return _emit({"error": "во входе нет страниц/изображений"}, ok=False)

    results: list[PageResult] = []
    words_by_page: list[list[dict]] = []
    for pg in raw_pages:
        proc, pmeta = preprocess(pg.image, args.preprocess)
        paras, mconf, wc, words = _ocr_page(proc, lang, args.psm)
        results.append(PageResult(pg.index, pg.source, paras, mconf, wc, pmeta))
        words_by_page.append(words)

    overall = round(float(np.mean([p.mean_conf for p in results])), 1) if results else 0.0

    title = os.path.splitext(os.path.basename(args.input))[0]
    try:
        _build_docx(results, args.out, title, flag_low_conf=not args.no_flag)
    except Exception as exc:  # noqa: BLE001
        return _emit({"error": f"не удалось собрать DOCX: {exc}"}, ok=False)

    if args.report:
        _write_report(args.report, args.input, args.out, lang, results, overall)

    if args.json:
        payload = {
            "input": os.path.abspath(args.input),
            "pages": [
                {"index": r.index, "source": r.source, "mean_conf": r.mean_conf,
                 "word_count": r.word_count, "preprocess": r.preprocess,
                 "words": words_by_page[i]}
                for i, r in enumerate(results)
            ],
        }
        tmp = args.json + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, args.json)

    total_words = sum(p.word_count for p in results)
    ok = total_words > 0
    return _emit({
        "input": os.path.abspath(args.input),
        "output": os.path.abspath(args.out),
        "report": os.path.abspath(args.report) if args.report else None,
        "json": os.path.abspath(args.json) if args.json else None,
        "lang": lang,
        "pages": len(results),
        "total_words": total_words,
        "overall_confidence": overall,
        "low_confidence_pages": [r.index + 1 for r in results if r.mean_conf < LOW_CONF_PAGE],
        "per_page": [
            {"page": r.index + 1, "words": r.word_count, "confidence": r.mean_conf,
             "preprocess": r.preprocess.get("steps", [])}
            for r in results
        ],
    }, ok=ok)


# --- CLI ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Офлайн-OCR: изображения/PDF/фото → DOCX")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="проверить окружение (движок, языки, пакеты)")
    c.add_argument("--lang", default=DEFAULT_LANG)
    c.set_defaults(func=cmd_check)

    o = sub.add_parser("ocr", help="распознать вход и собрать DOCX")
    o.add_argument("input", help="изображение или PDF")
    o.add_argument("--out", required=True, help="путь к результату .docx")
    o.add_argument("--report", default=None, help="путь к Markdown-отчёту")
    o.add_argument("--json", default=None, help="путь к JSON со словами и координатами")
    o.add_argument("--lang", default=DEFAULT_LANG, help="языки Tesseract, напр. rus+eng")
    o.add_argument("--preprocess", choices=["auto", "scan", "photo", "none"],
                   default="auto", help="режим предобработки (по умолчанию auto)")
    o.add_argument("--psm", type=int, default=3, help="режим сегментации Tesseract")
    o.add_argument("--pdf-dpi", type=int, default=300, help="DPI рендера страниц PDF")
    o.add_argument("--no-flag", action="store_true",
                   help="не помечать красным ненадёжные фрагменты")
    o.set_defaults(func=cmd_ocr)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
