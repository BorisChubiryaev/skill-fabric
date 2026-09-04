#!/usr/bin/env python3
"""Драйвер автоматизации SberBrowser (Chromium) через CDP с подробным логом.

Скилл sber-browser-flow. Агент пишет сценарий кодом, но использует этот драйвер —
и тогда КАЖДЫЙ шаг сам попадает в структурированный лог: действие, URL, селектор,
время, при падении — скриншот, снимок DOM, консоль браузера и сетевые ошибки.
Смысл лога — чтобы пользователь прислал его, а автор навыка по нему понял, что
именно сломалось, не видя экрана.

Подключение — к УЖЕ ОТКРЫТОМУ SberBrowser по Chrome DevTools Protocol, поэтому
все логины и куки пользователя на месте. Браузер драйвер не закрывает.

Зависимость только одна: playwright (`pip install playwright`). Свой Chromium
качать не нужно — используется тот, к которому подключаемся.

Пример сценария (агент пишет такой файл и запускает через run_flow.py):

    def run(s):                      # s — это Session
        s.goto("https://internal.example/reports")
        s.fill("#search", "квартальный")
        s.click("button[type=submit]")
        s.wait_for("table.results")
        s.download("a.export-xlsx", save_as="report.xlsx")
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from playwright.sync_api import sync_playwright, Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeout
except Exception as exc:  # pragma: no cover
    sync_playwright = None
    _PW_ERR = exc


DEFAULT_CDP = os.environ.get("SBER_CDP_URL", "http://localhost:9222")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^\w.-]+", "-", str(text))[:n].strip("-")
    return s or "step"


class FlowError(RuntimeError):
    """Ошибка шага сценария — уже залогирована со скриншотом и контекстом."""


class Session:
    """Логируемая сессия работы с браузером.

    Каждый публичный метод действия оборачивает вызов Playwright: пишет строку в
    run.log и событие в events.jsonl, замеряет время, а при ошибке снимает
    скриншот + DOM и поднимает FlowError с понятным сообщением.
    """

    def __init__(
        self,
        run_dir: str,
        cdp_url: str = DEFAULT_CDP,
        download_dir: str | None = None,
        context_index: int = 0,
        default_timeout_ms: int = 15000,
        screenshot_every_step: bool = False,
        log_fill_values: bool = False,
    ) -> None:
        if sync_playwright is None:  # pragma: no cover
            raise RuntimeError(f"playwright не установлен: {_PW_ERR}")
        self.run_dir = os.path.abspath(run_dir)
        self.cdp_url = cdp_url
        self.download_dir = os.path.abspath(download_dir or os.path.join(self.run_dir, "downloads"))
        self.context_index = context_index
        self.default_timeout_ms = default_timeout_ms
        self.screenshot_every_step = screenshot_every_step
        self.log_fill_values = log_fill_values

        self._seq = 0
        self._steps = 0
        self._failures = 0
        self._started = time.time()
        self._downloads: list[dict] = []
        self._console: list[dict] = []

        os.makedirs(self.run_dir, exist_ok=True)
        os.makedirs(self.download_dir, exist_ok=True)
        os.makedirs(os.path.join(self.run_dir, "screenshots"), exist_ok=True)
        os.makedirs(os.path.join(self.run_dir, "dom"), exist_ok=True)
        self._runlog = open(os.path.join(self.run_dir, "run.log"), "w", encoding="utf-8")
        self._events = open(os.path.join(self.run_dir, "events.jsonl"), "w", encoding="utf-8")
        self._netlog = open(os.path.join(self.run_dir, "network.jsonl"), "w", encoding="utf-8")
        self._conlog = open(os.path.join(self.run_dir, "console.jsonl"), "w", encoding="utf-8")

    # --- жизненный цикл ---

    def __enter__(self) -> "Session":
        self._log_line(f"=== запуск сессии {_now()} · CDP {self.cdp_url} ===")
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception as exc:  # noqa: BLE001
            self._log_line(f"[FATAL] не удалось подключиться по CDP: {exc}")
            self._teardown_files(status="cdp_connect_failed", fatal=str(exc))
            self._pw.stop()
            raise FlowError(
                f"Не удалось подключиться к браузеру по CDP ({self.cdp_url}). "
                "Проверьте, что SberBrowser запущен с флагом "
                "--remote-debugging-port=9222 (см. команду connect-check)."
            ) from exc

        ctxs = self._browser.contexts
        if not ctxs:
            self._ctx = self._browser.new_context(accept_downloads=True)
        else:
            idx = min(self.context_index, len(ctxs) - 1)
            self._ctx = ctxs[idx]
        try:
            self._ctx.set_default_timeout(self.default_timeout_ms)
        except Exception:
            pass

        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._attach_listeners()
        self._log_line(f"подключено · контекстов: {len(ctxs)} · страниц: "
                       f"{len(self._ctx.pages)} · старт URL: {self._safe_url()}")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        status = "ok" if exc_type is None else "failed"
        if exc_type is not None and not isinstance(exc, FlowError):
            # Непойманная ошибка не из действия драйвера — тоже фиксируем.
            self._capture_failure("unhandled", exc)
        self._teardown_files(status=status,
                             fatal=None if exc_type is None else str(exc))
        try:
            self._pw.stop()   # закрывает соединение, НЕ браузер пользователя
        except Exception:
            pass
        return False

    # --- слушатели браузера ---

    def _attach_listeners(self) -> None:
        def on_console(msg):
            rec = {"t": _now(), "type": msg.type, "text": msg.text[:500]}
            try:
                loc = msg.location
                rec["url"] = (loc or {}).get("url")
            except Exception:
                pass
            self._console.append(rec)
            self._conlog.write(json.dumps(rec, ensure_ascii=False) + "\n")

        def on_pageerror(err):
            rec = {"t": _now(), "type": "pageerror", "text": str(err)[:500]}
            self._console.append(rec)
            self._conlog.write(json.dumps(rec, ensure_ascii=False) + "\n")

        def on_requestfailed(req):
            rec = {"t": _now(), "kind": "requestfailed", "method": req.method,
                   "url": req.url[:300],
                   "error": (req.failure or "")[:200] if isinstance(req.failure, str)
                   else str(getattr(req, "failure", ""))[:200]}
            self._netlog.write(json.dumps(rec, ensure_ascii=False) + "\n")

        def on_response(resp):
            # Логируем только ошибки сервера, чтобы файл не распухал и не собирал
            # лишнего с внутренних страниц.
            if resp.status >= 400:
                rec = {"t": _now(), "kind": "http_error", "status": resp.status,
                       "method": resp.request.method, "url": resp.url[:300]}
                self._netlog.write(json.dumps(rec, ensure_ascii=False) + "\n")

        try:
            self.page.on("console", on_console)
            self.page.on("pageerror", on_pageerror)
            self.page.on("requestfailed", on_requestfailed)
            self.page.on("response", on_response)
        except Exception:
            pass

    # --- вспомогательное ---

    def _safe_url(self) -> str:
        try:
            return self.page.url
        except Exception:
            return "(нет страницы)"

    def _log_line(self, text: str) -> None:
        self._runlog.write(f"{_now()}  {text}\n")
        self._runlog.flush()

    def _emit(self, event: dict) -> None:
        self._events.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._events.flush()

    def _shot(self, tag: str) -> str | None:
        name = f"{self._seq:03d}-{_slug(tag)}.png"
        path = os.path.join(self.run_dir, "screenshots", name)
        try:
            self.page.screenshot(path=path, full_page=False)
            return os.path.join("screenshots", name)
        except Exception:
            return None

    def _dom(self, tag: str) -> str | None:
        name = f"{self._seq:03d}-{_slug(tag)}.html"
        path = os.path.join(self.run_dir, "dom", name)
        try:
            html = self.page.content()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(html[:500000])
            return os.path.join("dom", name)
        except Exception:
            return None

    def _capture_failure(self, action: str, exc: Exception) -> tuple[str | None, str | None]:
        shot = self._shot(f"{action}-FAIL")
        dom = self._dom(f"{action}-FAIL")
        return shot, dom

    @contextmanager
    def _action(self, action: str, target: Any = None, **extra):
        self._seq += 1
        self._steps += 1
        seq = self._seq
        start = time.time()
        url_before = self._safe_url()
        tgt = "" if target is None else f" · {target}"
        self._log_line(f"[{seq:03d}] {action}{tgt}")
        ev: dict[str, Any] = {"seq": seq, "t": _now(), "action": action,
                              "target": target, "url": url_before, **extra}
        try:
            yield ev
            ev["ok"] = True
            ev["ms"] = int((time.time() - start) * 1000)
            ev["url_after"] = self._safe_url()
            if self.screenshot_every_step:
                ev["screenshot"] = self._shot(action)
            self._emit(ev)
            self._log_line(f"      ок за {ev['ms']} мс")
        except (PWTimeout, PWError, AssertionError, Exception) as exc:  # noqa: BLE001
            self._failures += 1
            ev["ok"] = False
            ev["ms"] = int((time.time() - start) * 1000)
            ev["error"] = f"{type(exc).__name__}: {exc}"[:600]
            shot, dom = self._capture_failure(action, exc)
            ev["screenshot"] = shot
            ev["dom"] = dom
            ev["url_after"] = self._safe_url()
            self._emit(ev)
            self._log_line(f"      ОШИБКА: {ev['error']}")
            if shot:
                self._log_line(f"      скриншот: {shot}")
            raise FlowError(f"Шаг {seq} «{action}{tgt}» упал: {ev['error']}") from exc

    # --- действия: навигация и ввод ---

    def goto(self, url: str, wait_until: str = "load", timeout_ms: int | None = None):
        with self._action("goto", url):
            self.page.goto(url, wait_until=wait_until,
                           timeout=timeout_ms or self.default_timeout_ms)

    def click(self, selector: str, timeout_ms: int | None = None):
        with self._action("click", selector):
            self.page.click(selector, timeout=timeout_ms or self.default_timeout_ms)

    def fill(self, selector: str, value: str, timeout_ms: int | None = None):
        shown = value if self.log_fill_values else f"<{len(value)} симв.>"
        with self._action("fill", selector, value=shown):
            self.page.fill(selector, value, timeout=timeout_ms or self.default_timeout_ms)

    def type(self, selector: str, text: str, delay_ms: int = 0,
             timeout_ms: int | None = None):
        shown = text if self.log_fill_values else f"<{len(text)} симв.>"
        with self._action("type", selector, value=shown):
            self.page.type(selector, text, delay=delay_ms,
                           timeout=timeout_ms or self.default_timeout_ms)

    def press(self, selector: str, key: str, timeout_ms: int | None = None):
        with self._action("press", selector, key=key):
            self.page.press(selector, key, timeout=timeout_ms or self.default_timeout_ms)

    def select_option(self, selector: str, value: str, timeout_ms: int | None = None):
        with self._action("select_option", selector, value=value):
            self.page.select_option(selector, value,
                                    timeout=timeout_ms or self.default_timeout_ms)

    def check(self, selector: str, timeout_ms: int | None = None):
        with self._action("check", selector):
            self.page.check(selector, timeout=timeout_ms or self.default_timeout_ms)

    def wait_for(self, selector: str, state: str = "visible",
                 timeout_ms: int | None = None):
        with self._action("wait_for", selector, state=state):
            self.page.wait_for_selector(selector, state=state,
                                        timeout=timeout_ms or self.default_timeout_ms)

    def wait_for_url(self, url_pattern: str, timeout_ms: int | None = None):
        with self._action("wait_for_url", url_pattern):
            self.page.wait_for_url(url_pattern,
                                   timeout=timeout_ms or self.default_timeout_ms)

    def wait_ms(self, ms: int):
        with self._action("wait_ms", ms):
            self.page.wait_for_timeout(ms)

    # --- извлечение ---

    def text(self, selector: str, timeout_ms: int | None = None) -> str:
        with self._action("text", selector) as ev:
            val = self.page.text_content(selector,
                                         timeout=timeout_ms or self.default_timeout_ms) or ""
            ev["chars"] = len(val)
            return val.strip()

    def eval_js(self, expression: str, arg: Any = None):
        with self._action("eval_js", expression[:80]) as ev:
            result = self.page.evaluate(expression, arg)
            ev["result_type"] = type(result).__name__
            return result

    def extract(self, selector: str, fields: dict[str, str] | None = None) -> list[dict]:
        """Собрать данные по строкам selector. fields — {имя: под-селектор};
        без fields возвращает текст каждого элемента."""
        with self._action("extract", selector, fields=fields) as ev:
            script = """([sel, fields]) => {
                const rows = [...document.querySelectorAll(sel)];
                return rows.map(r => {
                    if (!fields) return {text: r.innerText.trim()};
                    const o = {};
                    for (const [k, s] of Object.entries(fields)) {
                        const el = r.querySelector(s);
                        o[k] = el ? el.innerText.trim() : null;
                    }
                    return o;
                });
            }"""
            data = self.page.evaluate(script, [selector, fields])
            ev["rows"] = len(data)
            return data

    # --- скачивание ---

    def download(self, trigger_selector: str, save_as: str | None = None,
                 timeout_ms: int | None = None) -> str:
        """Кликнуть по элементу и сохранить скачанный файл. Возвращает путь."""
        with self._action("download", trigger_selector, save_as=save_as) as ev:
            with self.page.expect_download(
                    timeout=timeout_ms or max(self.default_timeout_ms, 60000)) as info:
                self.page.click(trigger_selector)
            dl = info.value
            name = save_as or dl.suggested_filename or "download.bin"
            dest = os.path.join(self.download_dir, name)
            dl.save_as(dest)
            size = os.path.getsize(dest) if os.path.exists(dest) else 0
            rec = {"name": name, "path": dest, "size": size,
                   "suggested": dl.suggested_filename, "url": dl.url[:300]}
            self._downloads.append(rec)
            ev["file"] = name
            ev["size"] = size
            self._log_line(f"      файл сохранён: {name} ({size} байт)")
            return dest

    def download_via(self, trigger: Callable[[], None], save_as: str | None = None,
                     timeout_ms: int | None = None) -> str:
        """Как download, но триггер — произвольная функция (для сложных случаев)."""
        with self._action("download_via", save_as or "callable") as ev:
            with self.page.expect_download(
                    timeout=timeout_ms or max(self.default_timeout_ms, 60000)) as info:
                trigger()
            dl = info.value
            name = save_as or dl.suggested_filename or "download.bin"
            dest = os.path.join(self.download_dir, name)
            dl.save_as(dest)
            size = os.path.getsize(dest) if os.path.exists(dest) else 0
            self._downloads.append({"name": name, "path": dest, "size": size,
                                    "suggested": dl.suggested_filename})
            ev["file"] = name
            ev["size"] = size
            return dest

    # --- диагностика ---

    def screenshot(self, tag: str = "manual") -> str | None:
        with self._action("screenshot", tag) as ev:
            path = self._shot(tag)
            ev["screenshot"] = path
            return path

    @contextmanager
    def step(self, name: str):
        """Группа действий под именем — попадает в лог как отметка."""
        self._log_line(f"--- шаг: {name} ---")
        yield

    # --- завершение и отчёт ---

    def _teardown_files(self, status: str, fatal: str | None) -> None:
        duration = round(time.time() - self._started, 1)
        errors = [e for e in self._read_events() if not e.get("ok", True)]
        summary = {
            "status": status,
            "cdp_url": self.cdp_url,
            "started": datetime.fromtimestamp(self._started).astimezone().isoformat(timespec="seconds"),
            "duration_s": duration,
            "steps": self._steps,
            "failures": self._failures,
            "final_url": self._safe_url() if hasattr(self, "page") else None,
            "downloads": self._downloads,
            "console_errors": sum(1 for c in self._console
                                  if c.get("type") in ("error", "pageerror")),
            "fatal": fatal,
        }
        try:
            with open(os.path.join(self.run_dir, "summary.json"), "w", encoding="utf-8") as fh:
                json.dump(summary, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass
        self._write_report(summary, errors)
        for fh in (self._runlog, self._events, self._netlog, self._conlog):
            try:
                fh.close()
            except Exception:
                pass

    def _read_events(self) -> list[dict]:
        try:
            self._events.flush()
            with open(os.path.join(self.run_dir, "events.jsonl"), encoding="utf-8") as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except Exception:
            return []

    def _write_report(self, summary: dict, errors: list[dict]) -> None:
        L = []
        ok = summary["status"] == "ok"
        L.append(f"# Отчёт о прогоне сценария — {'УСПЕХ' if ok else 'СБОЙ'}\n")
        L.append(f"- Время: {summary['started']} · длительность {summary['duration_s']} с")
        L.append(f"- Шагов: {summary['steps']} · ошибок: {summary['failures']} · "
                 f"ошибок в консоли: {summary['console_errors']}")
        L.append(f"- Итоговый URL: `{summary['final_url']}`")
        if summary["downloads"]:
            L.append(f"- Скачано файлов: {len(summary['downloads'])}")
            for d in summary["downloads"]:
                L.append(f"    - `{d['name']}` — {d['size']} байт")
        if summary["fatal"]:
            L.append(f"\n**Фатальная ошибка:** {summary['fatal']}")
        L.append("")
        if errors:
            L.append("## Что упало\n")
            for e in errors:
                L.append(f"### Шаг {e.get('seq')}: {e.get('action')} "
                         f"`{e.get('target')}`")
                L.append(f"- URL на момент шага: `{e.get('url')}`")
                L.append(f"- Ошибка: `{e.get('error')}`")
                if e.get("screenshot"):
                    L.append(f"- Скриншот: `{e['screenshot']}`")
                if e.get("dom"):
                    L.append(f"- Снимок DOM: `{e['dom']}`")
                L.append(f"- Вероятная причина: {self._diagnose(e)}")
                L.append("")
        else:
            L.append("## Ошибок нет — все шаги прошли.\n")
        L.append("## Что прислать для разбора\n")
        L.append("Пришлите папку прогона целиком (или хотя бы `report.md`, "
                 "`run.log`, `events.jsonl`, `console.jsonl` и скриншоты из "
                 "`screenshots/`). По ним видно, на каком шаге и почему сломалось.\n")
        try:
            with open(os.path.join(self.run_dir, "report.md"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(L))
        except Exception:
            pass

    @staticmethod
    def _diagnose(e: dict) -> str:
        err = (e.get("error") or "").lower()
        act = e.get("action")
        if "timeout" in err and act in ("click", "fill", "type", "wait_for", "press"):
            return ("элемент не появился за отведённое время — селектор неверный, "
                    "либо страница не догрузилась, либо нужен вход/капча. "
                    "Сверьте селектор со снимком DOM.")
        if "timeout" in err and act == "goto":
            return ("страница не загрузилась — нет доступа к адресу, VPN/прокси "
                    "или неверный URL.")
        if "expect_download" in err or (act == "download" and "timeout" in err):
            return ("скачивание не началось — клик не по той кнопке, файл "
                    "открылся во вкладке вместо загрузки, или требуется "
                    "подтверждение.")
        if "not attached" in err or "detached" in err:
            return "элемент исчез из DOM (страница перерисовалась) — добавьте wait_for."
        if "net::" in err or "cdp" in err:
            return "сетевая проблема или разрыв соединения с браузером."
        return "смотрите скриншот и снимок DOM на момент ошибки."
