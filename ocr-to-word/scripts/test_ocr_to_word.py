"""Юнит-тесты инструмента ocr_to_word.

Запуск:  python3 -m unittest scripts/test_ocr_to_word.py -v
Требуют: бинарь tesseract (rus+eng), pytesseract, opencv, numpy, pillow, python-docx.
Тесты, зависящие от движка, самопропускаются, если tesseract недоступен.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "ocr_to_word.py")
sys.path.insert(0, HERE)
import ocr_to_word as m  # noqa: E402


def _tesseract_ready() -> bool:
    try:
        import pytesseract
        langs = set(pytesseract.get_languages(config=""))
        return {"rus", "eng"}.issubset(langs)
    except Exception:
        return False


READY = _tesseract_ready()


def _font(sz: int):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, sz)
    return ImageFont.load_default()


def _make_image(path: str, lines: list[str], skew: float = 0.0) -> None:
    im = Image.new("RGB", (1100, 500), "white")
    d = ImageDraw.Draw(im)
    f = _font(36)
    y = 40
    for ln in lines:
        d.text((50, y), ln, fill="black", font=f)
        y += 80
    if skew:
        arr = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
        h, w = arr.shape[:2]
        mat = cv2.getRotationMatrix2D((w / 2, h / 2), skew, 1.0)
        arr = cv2.warpAffine(arr, mat, (w, h), borderValue=(255, 255, 255))
        im = Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))
    im.save(path)


def _run(*args: str) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, {"_stdout": proc.stdout, "_stderr": proc.stderr}


LINES = ["Договор оказания услуг", "Total amount 45000 rubles"]


class PureLogicTests(unittest.TestCase):
    """Тесты, не требующие движка OCR."""

    def test_skew_sign_is_correct(self) -> None:
        # Изображение, наклонённое на +8°, должно давать корректирующий угол ≈ -8°.
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "s.png")
        _make_image(p, LINES, skew=8.0)
        gray = m._to_gray(cv2.imread(p))
        angle = m._estimate_skew(gray)
        self.assertLess(angle, -3.0)
        self.assertGreater(angle, -13.0)

    def test_preprocess_none_is_grayscale(self) -> None:
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "s.png")
        _make_image(p, LINES)
        proc, meta = m.preprocess(cv2.imread(p), "none")
        self.assertEqual(proc.ndim, 2)
        self.assertEqual(meta["steps"], ["grayscale"])

    def test_upscale_small_image(self) -> None:
        meta = {"steps": []}
        small = np.full((200, 300), 255, np.uint8)
        out = m._maybe_upscale(small, meta)
        self.assertEqual(out.shape, (400, 600))
        self.assertIn("upscale x2", meta["steps"])

    def test_photo_pipeline_normalizes_illumination(self) -> None:
        # Фото-ветка должна включать нормализацию освещённости (фикс виньетки).
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "s.png")
        _make_image(p, LINES)
        _, meta = m.preprocess(cv2.imread(p), "photo")
        self.assertIn("illumination", meta["steps"])

    def test_configure_tesseract_finds_binary_on_path(self) -> None:
        # Если tesseract на PATH, автопоиск должен его вернуть.
        import shutil
        if not shutil.which("tesseract"):
            self.skipTest("tesseract не на PATH")
        self.assertIsNotNone(m._configure_tesseract())


@unittest.skipUnless(READY, "tesseract с rus+eng недоступен")
class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.scan = os.path.join(self.tmp, "scan.png")
        self.photo = os.path.join(self.tmp, "photo.png")
        _make_image(self.scan, LINES)
        _make_image(self.photo, LINES, skew=7.0)

    def test_check_reports_ready(self) -> None:
        rc, out = _run("check")
        self.assertEqual(rc, 0)
        self.assertTrue(out["ready"])
        self.assertIn("rus", out["languages"])
        self.assertIn("eng", out["languages"])

    def test_ocr_clean_scan_to_docx(self) -> None:
        out_docx = os.path.join(self.tmp, "scan.docx")
        report = os.path.join(self.tmp, "scan.md")
        rc, out = _run("ocr", self.scan, "--out", out_docx, "--report", report)
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertGreater(out["total_words"], 3)
        self.assertGreater(out["overall_confidence"], 60)
        self.assertTrue(os.path.getsize(out_docx) > 0)
        self.assertTrue(os.path.getsize(report) > 0)
        from docx import Document
        text = "\n".join(p.text for p in Document(out_docx).paragraphs)
        self.assertIn("Договор", text)

    def test_ocr_skewed_photo_recovers_text(self) -> None:
        out_docx = os.path.join(self.tmp, "photo.docx")
        rc, out = _run("ocr", self.photo, "--out", out_docx, "--preprocess", "auto")
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        # Deskew должен сработать и вернуть текст, а не 0 слов.
        self.assertGreater(out["total_words"], 3)
        self.assertTrue(any("deskew" in s for s in out["per_page"][0]["preprocess"]))

    def test_json_has_words_and_boxes(self) -> None:
        out_docx = os.path.join(self.tmp, "j.docx")
        js = os.path.join(self.tmp, "j.json")
        _run("ocr", self.scan, "--out", out_docx, "--json", js)
        with open(js, encoding="utf-8") as fh:
            data = json.load(fh)
        w0 = data["pages"][0]["words"][0]
        for key in ("text", "conf", "left", "top", "width", "height"):
            self.assertIn(key, w0)

    def test_refuses_non_docx_out(self) -> None:
        rc, out = _run("ocr", self.scan, "--out", os.path.join(self.tmp, "x.txt"))
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])

    def test_missing_input(self) -> None:
        rc, out = _run("ocr", os.path.join(self.tmp, "nope.png"),
                       "--out", os.path.join(self.tmp, "o.docx"))
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])

    def test_unsupported_language_reports(self) -> None:
        rc, out = _run("ocr", self.scan, "--out",
                       os.path.join(self.tmp, "o.docx"), "--lang", "klingon")
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
