"""Тесты драйвера и раннера SberBrowser.

Запуск:  python3 -m unittest scripts/test_sber_flow.py -v

Тесты, требующие браузера, сами поднимают предустановленный Chromium с CDP и
локальный HTTP-сервер, поэтому SberBrowser для проверки не нужен. Если ни
Playwright, ни бинаря Chromium нет, эти тесты самопропускаются.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
RUN_FLOW = os.path.join(HERE, "run_flow.py")


def _have_playwright() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _find_chromium() -> str | None:
    import glob
    for pat in ("/opt/pw-browsers/chromium-*/chrome-linux/chrome",
                "/opt/pw-browsers/chromium-*/chrome-linux/headless_shell"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    for name in ("chromium", "chromium-browser", "google-chrome"):
        from shutil import which
        p = which(name)
        if p:
            return p
    return None


CHROMIUM = _find_chromium()
READY = _have_playwright() and CHROMIUM is not None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PAGE = """<!doctype html><meta charset=utf-8><title>Тест</title>
<h1 id=h>Форма</h1>
<input id=q>
<button id=go onclick="document.getElementById('out').textContent='нажато:'+document.getElementById('q').value">Искать</button>
<div id=out></div>
<ul id=list><li class=row><span class=name>Иван</span><span class=val>10</span></li>
<li class=row><span class=name>Мария</span><span class=val>20</span></li></ul>
<a id=dl href="data:text/plain;charset=utf-8,привет" download="f.txt">Скачать</a>
"""


@unittest.skipUnless(READY, "нет playwright или бинаря chromium")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp()
        # локальный сайт
        site = os.path.join(cls.tmp, "site")
        os.makedirs(site)
        with open(os.path.join(site, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(PAGE)
        cls.http_port = _free_port()
        handler = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=site, **k)
        cls.httpd = HTTPServer(("127.0.0.1", cls.http_port), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http_port}/index.html"

        # chromium с CDP
        cls.cdp_port = _free_port()
        cls.proc = subprocess.Popen(
            [CHROMIUM, "--headless=new", f"--remote-debugging-port={cls.cdp_port}",
             f"--user-data-dir={os.path.join(cls.tmp, 'profile')}",
             "--no-sandbox", "--no-first-run", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.cdp = f"http://127.0.0.1:{cls.cdp_port}"
        for _ in range(40):
            try:
                urllib.request.urlopen(cls.cdp + "/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.25)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.proc.terminate()
        except Exception:
            pass
        try:
            cls.httpd.shutdown()
        except Exception:
            pass

    def _run(self, *args: str) -> tuple[int, dict]:
        env = dict(os.environ, SBER_CDP_URL=self.cdp)
        p = subprocess.run([sys.executable, RUN_FLOW, *args],
                           capture_output=True, text=True, env=env)
        try:
            return p.returncode, json.loads(p.stdout)
        except json.JSONDecodeError:
            return p.returncode, {"_out": p.stdout, "_err": p.stderr}

    def _flow(self, body: str) -> str:
        path = os.path.join(self.tmp, f"flow_{abs(hash(body))}.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def test_connect_check_lists_browser(self) -> None:
        rc, out = self._run("connect-check", "--cdp", self.cdp)
        self.assertEqual(rc, 0)
        self.assertTrue(out["ready"])
        self.assertIn("Chrome", out["browser"])

    def test_connect_check_failure_gives_hint(self) -> None:
        rc, out = self._run("connect-check", "--cdp", "http://127.0.0.1:1")
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertIn("remote-debugging-port", out["hint"])

    def test_successful_flow_navigation_input_download(self) -> None:
        flow = self._flow(
            "def run(s):\n"
            f"    s.goto({self.base!r})\n"
            "    s.wait_for('#h')\n"
            "    s.fill('#q', 'квартальный отчёт')\n"
            "    s.click('#go')\n"
            "    assert 'квартальный' in s.text('#out')\n"
            "    s.download('#dl', save_as='f.txt')\n")
        out_dir = os.path.join(self.tmp, "runs_ok")
        rc, out = self._run("run", flow, "--cdp", self.cdp, "--out", out_dir)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["failures"], 0)
        self.assertEqual(out["downloads"][0]["name"], "f.txt")
        # файл реально сохранён и непустой
        self.assertGreater(out["downloads"][0]["size"], 0)
        # структура лога на месте
        rd = out["run_dir"]
        for f in ("run.log", "events.jsonl", "summary.json", "report.md"):
            self.assertTrue(os.path.getsize(os.path.join(rd, f)) > 0, f)

    def test_extract_returns_rows(self) -> None:
        flow = self._flow(
            "def run(s):\n"
            f"    s.goto({self.base!r})\n"
            "    rows = s.extract('#list .row', {'name': '.name', 'val': '.val'})\n"
            "    assert rows == [{'name':'Иван','val':'10'},{'name':'Мария','val':'20'}], rows\n")
        rc, out = self._run("run", flow, "--cdp", self.cdp,
                            "--out", os.path.join(self.tmp, "runs_ex"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["status"], "ok")

    def test_failure_produces_diagnostics(self) -> None:
        flow = self._flow(
            "def run(s):\n"
            f"    s.goto({self.base!r})\n"
            "    s.click('#does-not-exist', timeout_ms=2000)\n")
        rc, out = self._run("run", flow, "--cdp", self.cdp,
                            "--out", os.path.join(self.tmp, "runs_fail"))
        self.assertEqual(rc, 1)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["failures"], 1)
        rd = out["run_dir"]
        # скриншот и снимок DOM падения созданы
        shots = os.listdir(os.path.join(rd, "screenshots"))
        doms = os.listdir(os.path.join(rd, "dom"))
        self.assertTrue(any("FAIL" in x for x in shots), shots)
        self.assertTrue(any("FAIL" in x for x in doms), doms)
        # в отчёте есть вероятная причина
        with open(os.path.join(rd, "report.md"), encoding="utf-8") as fh:
            report = fh.read()
        self.assertIn("Вероятная причина", report)
        self.assertIn("does-not-exist", report)

    def test_fill_value_hidden_by_default(self) -> None:
        flow = self._flow(
            "def run(s):\n"
            f"    s.goto({self.base!r})\n"
            "    s.fill('#q', 'секретный-пароль-12345')\n")
        rc, out = self._run("run", flow, "--cdp", self.cdp,
                            "--out", os.path.join(self.tmp, "runs_mask"))
        self.assertEqual(rc, 0)
        with open(os.path.join(out["run_dir"], "events.jsonl"), encoding="utf-8") as fh:
            events = fh.read()
        self.assertNotIn("секретный-пароль", events)
        self.assertIn("симв.", events)


class OfflineTests(unittest.TestCase):
    """Не требуют браузера."""

    def test_new_flow_template_has_run(self) -> None:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "f.py")
        p = subprocess.run([sys.executable, RUN_FLOW, "new-flow", path],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("def run(s):", body)

    def test_diagnose_maps_timeout(self) -> None:
        import sber_driver as d
        msg = d.Session._diagnose({"action": "click",
                                   "error": "TimeoutError: Timeout 2000ms"})
        self.assertIn("селектор", msg.lower())

    def test_slug_sanitizes(self) -> None:
        import sber_driver as d
        self.assertEqual(d._slug("a b/c?d"), "a-b-c-d")
        self.assertEqual(d._slug(""), "step")


if __name__ == "__main__":
    unittest.main()
