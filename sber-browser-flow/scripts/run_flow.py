#!/usr/bin/env python3
"""Запуск сценариев автоматизации SberBrowser с логированием.

Команды:
  connect-check   — проверить подключение к браузеру по CDP, показать вкладки
                    и, если не подключилось, — как запустить SberBrowser.
  run <flow.py>   — выполнить сценарий (файл с функцией run(s)) внутри
                    логируемой сессии. Пишет папку прогона с логами и отчётом.
  new-flow <path> — создать заготовку сценария.

Сценарий — обычный Python-файл, который определяет функцию `run(s)`, где `s` —
это Session из sber_driver. Всё логирование берёт на себя раннер.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sber_driver import Session, FlowError, DEFAULT_CDP  # noqa: E402


def _emit(payload: dict, ok: bool) -> int:
    json.dump({"ok": bool(ok), **payload}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if ok else 1


LAUNCH_HINT_MAC = (
    "SberBrowser не отвечает по CDP. Закройте все его окна и запустите с флагом "
    "отладки, указав отдельный каталог профиля (чтобы не мешать обычному):\n"
    "  /Applications/SberBrowser.app/Contents/MacOS/SberBrowser \\\n"
    "    --remote-debugging-port=9222 \\\n"
    "    --user-data-dir=\"$HOME/.sber-automation-profile\"\n"
    "В этом окне один раз залогиньтесь на нужных сайтах — сессия сохранится. "
    "Точный путь к бинарю может отличаться; проверьте "
    "/Applications на предмет *.app и загляните в Contents/MacOS."
)


def cmd_connect_check(args: argparse.Namespace) -> int:
    url = args.cdp
    version_url = url.rstrip("/") + "/json/version"
    list_url = url.rstrip("/") + "/json/list"
    info: dict = {"cdp_url": url}
    try:
        with urllib.request.urlopen(version_url, timeout=4) as r:
            ver = json.load(r)
        info["browser"] = ver.get("Browser")
        info["has_ws"] = "webSocketDebuggerUrl" in ver
    except Exception as exc:  # noqa: BLE001
        return _emit({**info, "error": f"нет ответа по CDP: {exc}",
                      "hint": LAUNCH_HINT_MAC}, ok=False)
    tabs = []
    try:
        with urllib.request.urlopen(list_url, timeout=4) as r:
            for t in json.load(r):
                if t.get("type") == "page":
                    tabs.append({"title": t.get("title"), "url": t.get("url")})
    except Exception:
        pass
    info["tabs"] = tabs[:20]
    info["tab_count"] = len(tabs)
    info["ready"] = True
    return _emit(info, ok=True)


def _load_flow(path: str):
    spec = importlib.util.spec_from_file_location("user_flow", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "run"):
        raise AttributeError("в сценарии нет функции run(s)")
    return mod.run


def cmd_run(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.flow):
        return _emit({"error": f"файл сценария не найден: {args.flow}"}, ok=False)
    try:
        flow = _load_flow(args.flow)
    except Exception as exc:  # noqa: BLE001
        return _emit({"error": f"не удалось загрузить сценарий: {exc}"}, ok=False)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.abspath(args.out or "runs")
    run_dir = os.path.join(base, stamp)

    status, error = "ok", None
    try:
        with Session(
            run_dir=run_dir,
            cdp_url=args.cdp,
            download_dir=args.download_dir,
            context_index=args.context,
            default_timeout_ms=args.timeout,
            screenshot_every_step=args.shots,
            log_fill_values=args.show_values,
        ) as s:
            flow(s)
    except FlowError as exc:
        status, error = "failed", str(exc)
    except Exception as exc:  # noqa: BLE001
        status, error = "error", f"{type(exc).__name__}: {exc}"

    summary_path = os.path.join(run_dir, "summary.json")
    summary = {}
    if os.path.isfile(summary_path):
        with open(summary_path, encoding="utf-8") as fh:
            summary = json.load(fh)

    return _emit({
        "run_dir": run_dir,
        "status": summary.get("status", status),
        "error": error,
        "steps": summary.get("steps"),
        "failures": summary.get("failures"),
        "downloads": summary.get("downloads", []),
        "report": os.path.join(run_dir, "report.md"),
        "logs": {"run_log": os.path.join(run_dir, "run.log"),
                 "events": os.path.join(run_dir, "events.jsonl"),
                 "console": os.path.join(run_dir, "console.jsonl")},
    }, ok=(summary.get("status", status) == "ok"))


FLOW_TEMPLATE = '''"""Сценарий SberBrowser. Запуск:
  python3 <путь>/run_flow.py run <этот файл> --out runs

Доступные действия s (все логируются автоматически):
  s.goto(url)                     переход
  s.click(sel)                    клик
  s.fill(sel, text)               заполнить поле (значение в лог не пишется)
  s.type(sel, text)               ввод посимвольно
  s.press(sel, "Enter")           нажать клавишу
  s.select_option(sel, value)     выбрать из списка
  s.wait_for(sel)                 дождаться элемента
  s.wait_for_url("**/done")       дождаться URL
  s.text(sel) -> str              прочитать текст
  s.extract(sel, {"имя": подсел}) -> list[dict]   собрать таблицу
  s.download(sel, save_as="f.xlsx") -> путь        скачать по клику
  s.screenshot("метка")           ручной скриншот
  with s.step("название"): ...    пометить группу шагов
"""


def run(s):
    s.goto("https://example.com")
    s.wait_for("h1")
    # s.fill("#search", "запрос")
    # s.click("button[type=submit]")
    # s.download("a.export", save_as="report.xlsx")
'''


def cmd_new_flow(args: argparse.Namespace) -> int:
    if os.path.exists(args.path) and not args.force:
        return _emit({"error": f"файл уже существует: {args.path} "
                               "(перезаписать — с --force)"}, ok=False)
    os.makedirs(os.path.dirname(os.path.abspath(args.path)) or ".", exist_ok=True)
    with open(args.path, "w", encoding="utf-8") as fh:
        fh.write(FLOW_TEMPLATE)
    return _emit({"path": os.path.abspath(args.path),
                  "next": f"отредактируйте и запустите: run {args.path}"}, ok=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Автоматизация SberBrowser с логами")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("connect-check", help="проверить подключение по CDP")
    c.add_argument("--cdp", default=DEFAULT_CDP)
    c.set_defaults(func=cmd_connect_check)

    r = sub.add_parser("run", help="выполнить сценарий с логированием")
    r.add_argument("flow", help="python-файл с функцией run(s)")
    r.add_argument("--cdp", default=DEFAULT_CDP)
    r.add_argument("--out", default="runs", help="каталог для папок прогонов")
    r.add_argument("--download-dir", default=None)
    r.add_argument("--context", type=int, default=0,
                   help="индекс контекста браузера (окна), если их несколько")
    r.add_argument("--timeout", type=int, default=15000,
                   help="таймаут действий по умолчанию, мс")
    r.add_argument("--shots", action="store_true",
                   help="скриншот на КАЖДОМ шаге (по умолчанию только при ошибке)")
    r.add_argument("--show-values", action="store_true",
                   help="писать в лог вводимый текст (по умолчанию скрыт — там "
                        "могут быть пароли/ПДн)")
    r.set_defaults(func=cmd_run)

    n = sub.add_parser("new-flow", help="создать заготовку сценария")
    n.add_argument("path")
    n.add_argument("--force", action="store_true")
    n.set_defaults(func=cmd_new_flow)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
