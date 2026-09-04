#!/usr/bin/env python3
"""Глубокая аналитика Jira: сырые выгрузки → метрики → автономный HTML-дашборд.

Инструмент навыка jira-analytics. Рассчитан на Jira Server / Data Center.
Использует только стандартную библиотеку Python — ни сети, ни внешних пакетов:
данные из Jira приносит агент через MCP, а этот скрипт их считает и рисует.

Команды:
  normalize — сырые JSON-выгрузки (ответы Jira REST/MCP) → канонический issues.json
  analyze   — issues.json → metrics.json (метрики + предупреждения)
  render    — metrics.json → самодостаточный dashboard.html (без CDN)
  demo      — синтетический набор задач для проверки конвейера без Jira

Философия: считать честно и показывать неопределённость. Если changelog не
выгружен, cycle time посчитать нельзя — скрипт скажет об этом, а не подставит
правдоподобное число.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# --- Общее -------------------------------------------------------------------

DONE = "done"
WIP = "indeterminate"
TODO = "new"
CATEGORY_RU = {TODO: "К выполнению", WIP: "В работе", DONE: "Готово"}

# Статусы, которые по названию означают «заблокировано» (Jira DC, рус/англ).
BLOCKED_PATTERN = re.compile(
    r"\b(blocked|block|impediment|на\s*паузе|заблокирован|блокир)", re.I
)


def _emit(payload: dict[str, Any], ok: bool) -> int:
    json.dump({"ok": bool(ok), **payload}, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if ok else 1


def _atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def parse_dt(value: Any) -> datetime | None:
    """Разбирает даты Jira. DC отдаёт '2026-01-05T10:00:00.000+0300',
    иногда встречается 'Z' или голая дата 'YYYY-MM-DD'."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    # +0300 -> +03:00 (fromisoformat в 3.10 не принимает без двоеточия)
    m = re.search(r"([+-]\d{2})(\d{2})$", s)
    if m:
        s = s[: m.start()] + f"{m.group(1)}:{m.group(2)}"
    for attempt in (s, s.split(".")[0], s[:10]):
        try:
            dt = datetime.fromisoformat(attempt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def days_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 86400.0


def percentile(values: list[float], p: float) -> float | None:
    """Перцентиль методом ближайшего ранга — устойчив на малых выборках."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return round(xs[0], 2)
    k = max(0, min(len(xs) - 1, int(math.ceil(p / 100.0 * len(xs))) - 1))
    return round(xs[k], 2)


# --- normalize ---------------------------------------------------------------


def _iter_raw_issues(obj: Any) -> Iterable[dict]:
    """Достаёт задачи из разных форм ответа: {'issues':[...]}, [...],
    {'data':{'issues':[...]}} или одиночная задача."""
    if obj is None:
        return []
    if isinstance(obj, list):
        out = []
        for item in obj:
            out.extend(_iter_raw_issues(item))
        return out
    if isinstance(obj, dict):
        for key in ("issues", "results", "values"):
            if isinstance(obj.get(key), list):
                return _iter_raw_issues(obj[key])
        if isinstance(obj.get("data"), (dict, list)):
            nested = _iter_raw_issues(obj["data"])
            if nested:
                return nested
        if "key" in obj and ("fields" in obj or "status" in obj):
            return [obj]
    return []


def _sprint_names(fields: dict) -> list[str]:
    """Спринты в Jira DC лежат в customfield_* как строки вида
    '...[id=5,name=Sprint 3,...]' либо как объекты."""
    names: list[str] = []
    for k, v in fields.items():
        if not k.startswith("customfield_") or v is None:
            continue
        items = v if isinstance(v, list) else [v]
        for it in items:
            if isinstance(it, str) and "greenhopper.service.sprint" in it:
                m = re.search(r"name=([^,\]]+)", it)
                if m:
                    names.append(m.group(1).strip())
            elif isinstance(it, dict) and "name" in it and (
                "sprint" in str(it.get("self", "")).lower() or "state" in it
            ):
                names.append(str(it["name"]))
    return names


def _story_points(fields: dict, field_name: str | None) -> float | None:
    if field_name and isinstance(fields.get(field_name), (int, float)):
        return float(fields[field_name])
    for k in ("customfield_10002", "customfield_10004", "customfield_10016", "storyPoints"):
        v = fields.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _flagged(fields: dict) -> bool:
    for k, v in fields.items():
        if not k.startswith("customfield_") or v is None:
            continue
        items = v if isinstance(v, list) else [v]
        for it in items:
            val = it.get("value") if isinstance(it, dict) else it
            if isinstance(val, str) and re.search(r"impediment|flag", val, re.I):
                return True
    return False


ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def _epic_key(fields: dict, field_name: str | None) -> str | None:
    """Ссылка на эпик. В Jira DC она живёт в customfield «Epic Link», номер
    которого различается между инсталляциями, поэтому определяем по виду
    значения: голая строка-ключ задачи. Явное имя поля важнее эвристики."""
    if field_name:
        v = fields.get(field_name)
        if isinstance(v, str) and ISSUE_KEY_RE.match(v.strip()):
            return v.strip()
    epic = fields.get("epic")
    if isinstance(epic, dict) and epic.get("key"):
        return str(epic["key"])
    for k, v in fields.items():
        if k.startswith("customfield_") and isinstance(v, str) \
                and ISSUE_KEY_RE.match(v.strip()):
            return v.strip()
    return None


def _blocked_by(fields: dict) -> list[str]:
    out = []
    for link in fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        t = (link.get("type") or {})
        inward = str(t.get("inward", "")).lower()
        if "block" in inward and link.get("inwardIssue"):
            key = (link["inwardIssue"] or {}).get("key")
            if key:
                out.append(key)
    return out


def _changelog(raw: dict) -> list[dict]:
    """Плоский список смен статуса из changelog (expand=changelog)."""
    histories = ((raw.get("changelog") or {}).get("histories")) or raw.get("histories") or []
    events = []
    for h in histories:
        at = parse_dt(h.get("created"))
        if not at:
            continue
        for item in h.get("items") or []:
            if str(item.get("field", "")).lower() != "status":
                continue
            events.append({
                "at": iso(at),
                "from": item.get("fromString"),
                "to": item.get("toString"),
            })
    events.sort(key=lambda e: e["at"])
    return events


def normalize_issue(raw: dict, sp_field: str | None = None,
                    epic_field: str | None = None) -> dict:
    fields = raw.get("fields") or raw
    status = fields.get("status") or {}
    cat = ((status.get("statusCategory") or {}).get("key")
           or (status.get("statusCategory") or {}).get("name") or "").lower()
    if cat in ("done", "complete", "completed"):
        cat = DONE
    elif cat in ("indeterminate", "in progress", "inprogress"):
        cat = WIP
    elif cat in ("new", "to do", "todo"):
        cat = TODO
    else:
        cat = DONE if fields.get("resolutiondate") else TODO

    def name_of(x):
        if isinstance(x, dict):
            return x.get("displayName") or x.get("name") or x.get("value")
        return x

    sprints = _sprint_names(fields)
    itype = fields.get("issuetype") or {}
    parent = fields.get("parent") or {}
    return {
        "key": raw.get("key") or fields.get("key"),
        "summary": fields.get("summary"),
        "type": name_of(itype) or "Unknown",
        "is_subtask": bool(itype.get("subtask")) if isinstance(itype, dict) else False,
        "parent": parent.get("key") if isinstance(parent, dict) else None,
        "epic_key": _epic_key(fields, epic_field),
        "status": name_of(status) or "Unknown",
        "status_category": cat,
        "priority": name_of(fields.get("priority")),
        "assignee": name_of(fields.get("assignee")),
        "reporter": name_of(fields.get("reporter")),
        "created": iso(parse_dt(fields.get("created"))),
        "resolved": iso(parse_dt(fields.get("resolutiondate"))),
        "updated": iso(parse_dt(fields.get("updated"))),
        "duedate": iso(parse_dt(fields.get("duedate"))),
        "labels": list(fields.get("labels") or []),
        "components": [name_of(c) for c in (fields.get("components") or [])],
        "sprint": sprints[-1] if sprints else None,
        "story_points": _story_points(fields, sp_field),
        "flagged": _flagged(fields),
        "blocked_by": _blocked_by(fields),
        "changelog": _changelog(raw),
    }


def _load_transitions(path: str) -> dict[str, list[dict]]:
    """Внешняя история статусов: {"KEY-1": [{"at","from","to"}, ...]} либо
    плоский список записей с полем key/issue. Нужна, когда MCP не отдаёт
    changelog вместе с задачами."""
    with open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    out: dict[str, list[dict]] = defaultdict(list)

    def add(key, e):
        out[key].append({"at": e.get("at") or e.get("created"),
                         "from": e.get("from") or e.get("fromString"),
                         "to": e.get("to") or e.get("toString")})

    if isinstance(obj, dict):
        for key, evs in obj.items():
            for e in evs or []:
                add(key, e)
    elif isinstance(obj, list):
        for e in obj:
            key = e.get("key") or e.get("issue")
            if key:
                add(key, e)
    for key in list(out):
        out[key] = sorted((e for e in out[key] if e.get("at")),
                          key=lambda e: str(e["at"]))
    return dict(out)


def cmd_normalize(args: argparse.Namespace) -> int:
    issues: list[dict] = []
    seen: set[str] = set()
    missing: list[str] = []
    for path in args.inputs:
        if not os.path.isfile(path):
            missing.append(path)
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            # JSONL: по объекту на строку
            obj = []
            for line in text.splitlines():
                line = line.strip()
                if line:
                    try:
                        obj.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        for raw in _iter_raw_issues(obj):
            norm = normalize_issue(raw, args.story_points_field, args.epic_link_field)
            if not norm["key"] or norm["key"] in seen:
                continue
            seen.add(norm["key"])
            issues.append(norm)

    if missing:
        return _emit({"error": "файлы не найдены: " + ", ".join(missing)}, ok=False)

    linked_from_file = 0
    if getattr(args, "epic_links", None):
        if not os.path.isfile(args.epic_links):
            return _emit({"error": f"файл связей не найден: {args.epic_links}"},
                         ok=False)
        with open(args.epic_links, encoding="utf-8") as fh:
            raw_links = json.load(fh)
        links: dict[str, str] = {}
        if isinstance(raw_links, dict):
            links = {k: v for k, v in raw_links.items() if isinstance(v, str)}
        elif isinstance(raw_links, list):
            for e in raw_links:
                k, v = e.get("key"), e.get("epic") or e.get("epic_key")
                if k and v:
                    links[k] = v
        for issue in issues:
            if links.get(issue["key"]) and not issue.get("epic_key"):
                issue["epic_key"] = links[issue["key"]]
                linked_from_file += 1

    # Иерархия: сначала связи из файла (см. выше), затем родитель и наследование.
    epic_keys = {i["key"] for i in issues if (i.get("type") or "").lower() == "epic"}
    linked_via_parent = 0
    for issue in issues:
        if not issue.get("epic_key") and issue.get("parent") in epic_keys:
            issue["epic_key"] = issue["parent"]
            linked_via_parent += 1
    # Подзадача наследует эпик своей родительской задачи: работа по ней
    # относится к тому же эпику, хотя Epic Link у неё обычно пуст.
    by_key = {i["key"]: i for i in issues}
    inherited = 0
    for issue in issues:
        if not issue.get("epic_key") and issue.get("parent"):
            parent = by_key.get(issue["parent"])
            if parent and parent.get("epic_key"):
                issue["epic_key"] = parent["epic_key"]
                inherited += 1
    linked_via_parent += inherited


    merged_from_file = 0
    if getattr(args, "transitions", None):
        if not os.path.isfile(args.transitions):
            return _emit({"error": f"файл переходов не найден: {args.transitions}"},
                         ok=False)
        trans = _load_transitions(args.transitions)
        for issue in issues:
            if not issue["changelog"] and trans.get(issue["key"]):
                issue["changelog"] = [
                    {"at": iso(parse_dt(e["at"])), "from": e.get("from"),
                     "to": e.get("to")}
                    for e in trans[issue["key"]] if parse_dt(e["at"])
                ]
                if issue["changelog"]:
                    merged_from_file += 1
    if not issues:
        return _emit({"error": "во входных файлах не найдено ни одной задачи "
                               "(ожидается ответ Jira search с полем issues)"}, ok=False)

    with_cl = sum(1 for i in issues if i["changelog"])
    _atomic_write(args.out, json.dumps(issues, ensure_ascii=False, indent=2))
    return _emit({
        "output": os.path.abspath(args.out),
        "issues": len(issues),
        "with_changelog": with_cl,
        "changelog_merged_from_file": merged_from_file,
        "hierarchy": {
            "epics": len(epic_keys),
            "subtasks": sum(1 for i in issues if i.get("is_subtask")),
            "linked_to_epic": sum(1 for i in issues if i.get("epic_key")),
            "linked_via_parent": linked_via_parent,
            "linked_from_file": linked_from_file,
            "orphans": sum(1 for i in issues
                           if not i.get("epic_key")
                           and (i.get("type") or "").lower() != "epic"),
        },
        "changelog_coverage_pct": round(100.0 * with_cl / len(issues), 1),
        "note": ("ни у одной задачи нет changelog — время цикла посчитать не "
                 "получится; выгрузите задачи с expand=changelog" if with_cl == 0
                 else "changelog есть не у всех задач — время цикла будет "
                      "посчитано только по ним" if with_cl < len(issues)
                 else "changelog есть у всех задач"),
    }, ok=True)


# --- analyze -----------------------------------------------------------------


def bucket_key(dt: datetime, gran: str) -> str:
    if gran == "day":
        return dt.strftime("%Y-%m-%d")
    if gran == "month":
        return dt.strftime("%Y-%m")
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def bucket_range(start: datetime, end: datetime, gran: str) -> list[str]:
    out, cur = [], start
    step = {"day": timedelta(days=1), "week": timedelta(days=7)}.get(gran)
    seen = set()
    while cur <= end:
        k = bucket_key(cur, gran)
        if k not in seen:
            seen.add(k)
            out.append(k)
        cur = cur + step if step else (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
    return out


def first_wip_time(issue: dict, wip_statuses: set[str],
                   done_statuses: set[str] | None = None
                   ) -> tuple[datetime | None, str | None]:
    """Момент начала работы и способ, которым он определён.

    Точный путь — первый переход в статус, известный как «в работе». Но набор
    таких статусов собирается по ТЕКУЩИМ статусам выборки, а если команда всё
    закрыла (WIP = 0), ни одного рабочего статуса среди текущих нет — и тогда
    время цикла молча оказывалось непосчитанным. Поэтому есть запасной путь:
    работой считаем первый переход, который не ведёт сразу в «готово».
    Возвращаем и способ, чтобы отчёт мог честно сказать, как это посчитано."""
    cl = issue.get("changelog") or []
    lower_wip = {x.lower() for x in wip_statuses}
    for ev in cl:
        to = (ev.get("to") or "").strip()
        if to and to.lower() in lower_wip:
            return parse_dt(ev["at"]), "exact"
    lower_done = {x.lower() for x in (done_statuses or set())}
    for ev in cl:
        to = (ev.get("to") or "").strip()
        if to and to.lower() not in lower_done:
            return parse_dt(ev["at"]), "fallback"
    return None, None


# Статусы ожидания: время в них — это не работа, а простой. Именно так
# «задача на 574 дня» оказывается задачей, две трети пролежавшей в Need Info.
WAITING_PATTERN = re.compile(
    r"(need\s*info|ожид|пауз|hold|blocked|заблок|на\s*согласован|уточн|"
    r"waiting|pending)", re.I
)
# Порог «вечной» незавершёнки: контейнеры-эпики, живущие годами, ломают
# перцентили и CFD, поэтому считаются отдельно от обычного зависания.
ETERNAL_WIP_DAYS = 180.0


def status_durations(issue: dict, now: datetime) -> dict[str, float]:
    """Сколько дней задача провела в каждом статусе (по changelog).
    Начальный статус берём из поля `from` первого перехода."""
    cl = issue.get("changelog") or []
    created = parse_dt(issue.get("created"))
    if not cl or not created:
        return {}
    end = parse_dt(issue.get("resolved")) or now
    out: dict[str, float] = defaultdict(float)
    cur = (cl[0].get("from") or "—").strip() or "—"
    cur_t = created
    for ev in cl:
        t = parse_dt(ev.get("at"))
        if not t:
            continue
        out[cur] += max(0.0, days_between(cur_t, t))
        cur = (ev.get("to") or cur).strip() or cur
        cur_t = t
    out[cur] += max(0.0, days_between(cur_t, end))
    return dict(out)


def detect_batch_closures(issues: list[dict], min_batch: int) -> list[dict]:
    """Пачковые закрытия: много задач закрыто в одно и то же время.
    Признак того, что статусы двигают задним числом, а метрики потока
    отражают учёт, а не реальную работу."""
    by_hour: dict[str, list[str]] = defaultdict(list)
    for i in issues:
        r = parse_dt(i.get("resolved"))
        if r:
            by_hour[r.strftime("%Y-%m-%d %H:00")].append(i["key"])
    return sorted(
        ({"at": h, "count": len(keys), "keys": keys[:20]}
         for h, keys in by_hour.items() if len(keys) >= min_batch),
        key=lambda b: -b["count"])


def detect_instant_closures(issues: list[dict], max_hours: float = 1.0) -> list[dict]:
    """Задачи, созданные и закрытые почти мгновенно: они занижают перцентили,
    хотя реальная работа шла вне Jira (или задача заведена постфактум)."""
    out = []
    for i in issues:
        c, r = parse_dt(i.get("created")), parse_dt(i.get("resolved"))
        if c and r and 0 <= days_between(c, r) * 24 <= max_hours:
            out.append({"key": i["key"], "summary": (i.get("summary") or "")[:120],
                        "assignee": i.get("assignee"),
                        "minutes": round(days_between(c, r) * 1440, 1)})
    return sorted(out, key=lambda x: x["minutes"])


def category_at(issue: dict, at: datetime, status_cat: dict[str, str]) -> str | None:
    """Категория задачи на момент `at` — по changelog, иначе по created/resolved."""
    created = parse_dt(issue["created"])
    if not created or created > at:
        return None
    cl = issue.get("changelog") or []
    if cl:
        cat = TODO
        for ev in cl:
            ts = parse_dt(ev["at"])
            if not ts or ts > at:
                break
            cat = status_cat.get((ev.get("to") or "").strip(), cat)
        return cat
    resolved = parse_dt(issue["resolved"])
    if resolved and resolved <= at:
        return DONE
    return issue["status_category"] if issue["status_category"] != DONE else WIP


def _is_blocked(issue: dict) -> bool:
    return bool(
        issue.get("flagged")
        or issue.get("blocked_by")
        or BLOCKED_PATTERN.search(issue.get("status") or "")
    )


def build_epic_rollup(all_issues: list[dict], epic_keys: set[str],
                      now: datetime, stale_days: int) -> list[dict]:
    """Прогресс по эпикам: сколько детей, сколько сделано, где затык.
    Именно этот срез отвечает на вопрос руководителя «что с инициативой»,
    которого не видно ни в одной метрике потока."""
    by_key = {i["key"]: i for i in all_issues}
    children: dict[str, list[dict]] = defaultdict(list)
    for i in all_issues:
        if i.get("epic_key"):
            children[i["epic_key"]].append(i)

    rows = []
    for ek in sorted(set(epic_keys) | set(children)):
        epic = by_key.get(ek)
        kids = children.get(ek, [])
        done = sum(1 for k in kids if k["status_category"] == DONE)
        wip = sum(1 for k in kids if k["status_category"] == WIP)
        todo = sum(1 for k in kids if k["status_category"] == TODO)
        blocked = sum(1 for k in kids if _is_blocked(k))
        last_act = max((parse_dt(k.get("updated")) for k in kids
                        if parse_dt(k.get("updated"))), default=None)
        if epic and parse_dt(epic.get("updated")):
            last_act = max(last_act or parse_dt(epic["updated"]),
                           parse_dt(epic["updated"]))
        created = parse_dt(epic.get("created")) if epic else None
        flags = []
        if epic and epic["status_category"] != DONE and not kids:
            flags.append("нет ни одной задачи")
        if kids and done == len(kids) and epic and epic["status_category"] != DONE:
            flags.append("все задачи сделаны, но эпик открыт")
        if epic and epic["status_category"] == DONE and (wip + todo) > 0:
            flags.append("эпик закрыт, но задачи открыты")
        if kids and done == 0 and epic and epic["status_category"] == WIP:
            flags.append("в работе, но ни одна задача не сделана")
        if last_act and days_between(last_act, now) >= stale_days and \
                epic and epic["status_category"] != DONE:
            flags.append(f"без движения {int(days_between(last_act, now))} дн.")
        rows.append({
            "key": ek,
            "summary": ((epic.get("summary") if epic else None)
                        or "(эпик вне выборки)")[:120],
            "status": epic.get("status") if epic else "—",
            "assignee": epic.get("assignee") if epic else None,
            "children": len(kids), "done": done, "wip": wip, "todo": todo,
            "blocked": blocked,
            "progress_pct": round(100.0 * done / len(kids), 1) if kids else 0.0,
            "age_days": round(days_between(created, now), 1) if created else None,
            "idle_days": (round(days_between(last_act, now), 1)
                          if last_act else None),
            "flags": flags,
        })
    rows.sort(key=lambda r: (-len(r["flags"]), -r["children"]))
    return rows


def cmd_analyze(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.input):
        return _emit({"error": f"файл не найден: {args.input}"}, ok=False)
    with open(args.input, encoding="utf-8") as fh:
        issues = json.load(fh)
    if not isinstance(issues, list) or not issues:
        return _emit({"error": "issues.json пуст или неверного формата"}, ok=False)

    now = parse_dt(args.now) or datetime.now(timezone.utc)
    gran = args.granularity

    # Период анализа: по умолчанию — от самой ранней даты создания до now.
    created_dts = [parse_dt(i["created"]) for i in issues if i.get("created")]
    created_dts = [d for d in created_dts if d]
    period_from = parse_dt(args.since) or (min(created_dts) if created_dts else now)
    period_to = parse_dt(args.until) or now

    all_issues = issues
    epic_keys = {i["key"] for i in issues if (i.get("type") or "").lower() == "epic"}
    if args.scope_items == "leaf" and epic_keys:
        # Эпик — контейнер, а не единица работы: считать его наравне с задачами
        # значит завышать объём и тянуть вверх хвост времени цикла.
        issues = [i for i in issues if i["key"] not in epic_keys]
        if not issues:
            issues = all_issues

    # Карта «название статуса → категория» из наблюдаемых данных.
    status_cat: dict[str, str] = {}
    for i in issues:
        if i.get("status"):
            status_cat[i["status"]] = i["status_category"]
    wip_statuses = {s for s, c in status_cat.items() if c == WIP}
    if args.wip_statuses:
        wip_statuses |= {x.strip() for x in args.wip_statuses.split(",") if x.strip()}
    done_statuses = {s for s, c in status_cat.items() if c == DONE}

    in_period = [
        i for i in issues
        if (d := parse_dt(i.get("created"))) and period_from <= d <= period_to
    ] or issues

    resolved_in_period = [
        i for i in issues
        if (d := parse_dt(i.get("resolved"))) and period_from <= d <= period_to
    ]

    buckets = bucket_range(period_from, period_to, gran)
    bidx = {b: n for n, b in enumerate(buckets)}

    created_series = [0] * len(buckets)
    resolved_series = [0] * len(buckets)
    for i in issues:
        d = parse_dt(i.get("created"))
        if d and period_from <= d <= period_to:
            k = bucket_key(d, gran)
            if k in bidx:
                created_series[bidx[k]] += 1
        r = parse_dt(i.get("resolved"))
        if r and period_from <= r <= period_to:
            k = bucket_key(r, gran)
            if k in bidx:
                resolved_series[bidx[k]] += 1

    # --- Времена ---
    lead_times, cycle_times = [], []
    cycle_methods: dict[str, int] = {}
    cycle_by_assignee: dict[str, list[float]] = defaultdict(list)
    for i in resolved_in_period:
        c, r = parse_dt(i["created"]), parse_dt(i["resolved"])
        if c and r and r >= c:
            lead_times.append(days_between(c, r))
        w, how = first_wip_time(i, wip_statuses, done_statuses)
        if w and r and r >= w:
            ct = days_between(w, r)
            cycle_times.append(ct)
            cycle_methods[how] = cycle_methods.get(how, 0) + 1
            if i.get("assignee"):
                cycle_by_assignee[i["assignee"]].append(ct)

    def pct_block(vals: list[float]) -> dict[str, Any]:
        return {
            "count": len(vals),
            "p50": percentile(vals, 50), "p75": percentile(vals, 75),
            "p85": percentile(vals, 85), "p95": percentile(vals, 95),
            "avg": round(sum(vals) / len(vals), 2) if vals else None,
            "max": round(max(vals), 2) if vals else None,
        }

    lead = pct_block(lead_times)
    cycle = pct_block(cycle_times)
    cl_cov = sum(1 for i in issues if i.get("changelog"))
    changelog_coverage = round(100.0 * cl_cov / len(issues), 1)

    # --- CFD и WIP по времени ---
    cfd = {TODO: [], WIP: [], DONE: []}
    wip_series = []
    for b in buckets:
        at = parse_dt(b) or now
        at = at.replace(hour=23, minute=59, tzinfo=timezone.utc)
        counts = Counter()
        for i in issues:
            c = category_at(i, at, status_cat)
            if c:
                counts[c] += 1
        for cat in (TODO, WIP, DONE):
            cfd[cat].append(counts.get(cat, 0))
        wip_series.append(counts.get(WIP, 0))

    # --- Люди ---
    open_issues = [i for i in issues if i["status_category"] != DONE]
    people: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"open": 0, "wip": 0, "resolved": 0, "points": 0.0}
    )
    for i in open_issues:
        a = i.get("assignee") or "— не назначен —"
        people[a]["open"] += 1
        if i["status_category"] == WIP:
            people[a]["wip"] += 1
    for i in resolved_in_period:
        a = i.get("assignee") or "— не назначен —"
        people[a]["resolved"] += 1
        if i.get("story_points"):
            people[a]["points"] += float(i["story_points"])

    people_rows = []
    for name, v in people.items():
        cts = cycle_by_assignee.get(name, [])
        people_rows.append({
            "assignee": name, "open": v["open"], "wip": v["wip"],
            "resolved": v["resolved"], "points": round(v["points"], 1),
            "cycle_p50": percentile(cts, 50),
        })
    people_rows.sort(key=lambda r: (-r["resolved"], -r["open"], r["assignee"]))

    total_resolved = sum(r["resolved"] for r in people_rows) or 1
    contributors = [r for r in people_rows
                    if r["resolved"] > 0 and r["assignee"] != "— не назначен —"]
    top_share = round(100.0 * contributors[0]["resolved"] / total_resolved, 1) if contributors else 0.0
    # Bus factor: сколько людей набирают 50% выполненного.
    bus, acc = 0, 0
    for r in contributors:
        acc += r["resolved"]
        bus += 1
        if acc >= total_resolved * 0.5:
            break

    # --- Риски ---
    stale_days, longlived_days = args.stale_days, args.longlived_days
    aging_threshold = cycle["p85"] or float(args.aging_days)

    def brief(i: dict, extra: dict | None = None) -> dict:
        d = {"key": i["key"], "summary": (i.get("summary") or "")[:120],
             "status": i.get("status"), "assignee": i.get("assignee")}
        if extra:
            d.update(extra)
        return d

    stale, blocked, overdue, aging, longlived, eternal = [], [], [], [], [], []
    for i in open_issues:
        upd, crt = parse_dt(i.get("updated")), parse_dt(i.get("created"))
        if upd and days_between(upd, now) >= stale_days:
            stale.append(brief(i, {"days_idle": round(days_between(upd, now), 1)}))
        if _is_blocked(i):
            blocked.append(brief(i, {"blocked_by": i.get("blocked_by") or None,
                                     "flagged": bool(i.get("flagged"))}))
        due = parse_dt(i.get("duedate"))
        if due and due < now:
            overdue.append(brief(i, {"overdue_days": round(days_between(due, now), 1)}))
        if crt and days_between(crt, now) >= longlived_days:
            longlived.append(brief(i, {"age_days": round(days_between(crt, now), 1)}))
        if i["status_category"] == WIP:
            w = first_wip_time(i, wip_statuses, done_statuses)[0] or crt
            if w:
                age = days_between(w, now)
                if age >= ETERNAL_WIP_DAYS:
                    # Такие «задачи» почти всегда контейнеры (эпики), а не работа:
                    # держим их отдельно, иначе они искажают и CFD, и перцентили.
                    eternal.append(brief(i, {"in_progress_days": round(age, 1),
                                             "type": i.get("type")}))
                elif age >= aging_threshold:
                    aging.append(brief(i, {"in_progress_days": round(age, 1)}))

    for lst, k in ((stale, "days_idle"), (overdue, "overdue_days"),
                   (aging, "in_progress_days"), (longlived, "age_days"),
                   (eternal, "in_progress_days")):
        lst.sort(key=lambda x: -x[k])

    # --- Время ожидания и эффективность потока ---
    waiting_total: dict[str, float] = defaultdict(float)
    per_issue_wait: list[dict] = []
    flow_eff: list[float] = []
    for i in issues:
        durs = status_durations(i, now)
        if not durs:
            continue
        total = sum(durs.values())
        wait = sum(d for st, d in durs.items() if WAITING_PATTERN.search(st))
        for st, d in durs.items():
            waiting_total[st] += d
        if total > 0:
            if wait > 0:
                per_issue_wait.append({
                    "key": i["key"], "summary": (i.get("summary") or "")[:120],
                    "waiting_days": round(wait, 1), "total_days": round(total, 1),
                    "waiting_share_pct": round(100.0 * wait / total, 1)})
            if i["status_category"] == DONE:
                flow_eff.append(100.0 * max(0.0, total - wait) / total)
    per_issue_wait.sort(key=lambda x: -x["waiting_days"])
    top_statuses = sorted(waiting_total.items(), key=lambda kv: -kv[1])[:12]

    # --- Качество данных ---
    batches = detect_batch_closures(issues, args.batch_min)
    instant = detect_instant_closures(issues)

    epics = build_epic_rollup(all_issues, epic_keys, now, stale_days)

    metrics = {
        "generated_at": iso(now),
        "period": {"from": iso(period_from), "to": iso(period_to), "granularity": gran},
        "scope": {
            "total_issues": len(issues),
            "created_in_period": len(in_period),
            "resolved_in_period": len(resolved_in_period),
            "open_now": len(open_issues),
            "changelog_coverage_pct": changelog_coverage,
        },
        "flow": {
            "buckets": buckets,
            "created": created_series,
            "resolved": resolved_series,
            "net": [c - r for c, r in zip(created_series, resolved_series)],
            "wip_over_time": wip_series,
            "cfd": cfd,
            "throughput_avg": round(sum(resolved_series) / len(buckets), 2) if buckets else 0,
            "lead_time": lead,
            "cycle_time": cycle,
            "cycle_time_method": {
                "exact": cycle_methods.get("exact", 0),
                "fallback": cycle_methods.get("fallback", 0),
                "note": ("начало работы определено по известным рабочим статусам"
                         if not cycle_methods.get("fallback") else
                         "у части задач рабочий статус неизвестен: началом "
                         "работы считался первый переход не в «готово». "
                         "Задайте --wip-statuses для точного расчёта"),
            },
        },
        "people": {
            "rows": people_rows,
            "contributors": len(contributors),
            "top_share_pct": top_share,
            "bus_factor": bus,
            "unassigned_open": sum(1 for i in open_issues if not i.get("assignee")),
        },
        "waiting": {
            "by_status_days": [{"status": st, "days": round(d, 1)}
                               for st, d in top_statuses],
            "top_issues": per_issue_wait[:25],
            "flow_efficiency_pct": (round(sum(flow_eff) / len(flow_eff), 1)
                                    if flow_eff else None),
        },
        "data_quality": {
            "batch_closures": batches[:10],
            "batch_min": args.batch_min,
            "instant_closures": instant[:25],
            "instant_count": len(instant),
        },
        "risks": {
            "stale": stale, "blocked": blocked, "overdue": overdue,
            "aging_wip": aging, "long_lived": longlived, "eternal_wip": eternal,
            "aging_threshold_days": round(aging_threshold, 1),
            "stale_days": stale_days, "longlived_days": longlived_days,
        },
        "epics": epics,
        "hierarchy": {
            "scope_items": args.scope_items,
            "epics_total": len(epic_keys),
            "epics_excluded_from_flow": (len(epic_keys)
                                         if args.scope_items == "leaf" else 0),
            "subtasks": sum(1 for i in all_issues if i.get("is_subtask")),
            "orphans": sum(1 for i in all_issues
                           if not i.get("epic_key") and i["key"] not in epic_keys),
            "counted_items": len(issues),
        },
        "status_distribution": dict(Counter(i["status"] for i in open_issues)),
        "type_distribution": dict(Counter(i["type"] for i in issues)),
    }
    metrics["warnings"] = build_warnings(metrics, args)
    _atomic_write(args.out, json.dumps(metrics, ensure_ascii=False, indent=2))
    return _emit({
        "output": os.path.abspath(args.out),
        "issues": len(issues),
        "period": metrics["period"],
        "warnings": len(metrics["warnings"]),
        "critical": sum(1 for w in metrics["warnings"] if w["severity"] == "critical"),
        "changelog_coverage_pct": changelog_coverage,
    }, ok=True)


def build_warnings(m: dict, args: argparse.Namespace) -> list[dict]:
    """Детерминированные правила → находки с уровнем и доказательствами.
    Каждое правило объясняет, почему это важно, чтобы вывод был действием,
    а не просто числом."""
    out: list[dict] = []

    def add(sev: str, title: str, detail: str, evidence: Any = None) -> None:
        out.append({"severity": sev, "title": title, "detail": detail,
                    "evidence": evidence})

    flow, people, risks, scope = m["flow"], m["people"], m["risks"], m["scope"]

    # Приток против оттока: устойчивый рост бэклога.
    created = scope["created_in_period"]
    resolved = scope["resolved_in_period"]
    if resolved and created > resolved * 1.2:
        add("serious", "Бэклог растёт быстрее, чем закрывается",
            f"За период создано {created} задач, закрыто {resolved} "
            f"(+{created - resolved}). При таком темпе очередь будет только расти — "
            "стоит либо сократить приток, либо усилить команду.",
            {"created": created, "resolved": resolved})
    elif resolved > created * 1.2 and created:
        add("good", "Команда разгребает бэклог",
            f"Закрыто {resolved} против {created} созданных — очередь сокращается.",
            {"created": created, "resolved": resolved})

    # Разгон времени цикла: вторая половина против первой.
    half = len(flow["buckets"]) // 2
    if half >= 2:
        a, b = sum(flow["resolved"][:half]), sum(flow["resolved"][half:])
        if a and b < a * 0.6:
            add("warning", "Пропускная способность падает",
                f"В первой половине периода закрыто {a} задач, во второй — {b} "
                f"(−{round(100 * (1 - b / a))}%). Проверьте, не выросли ли "
                "блокеры или объём незавершёнки.",
                {"first_half": a, "second_half": b})

    # Незавершёнка на человека.
    overloaded = [r for r in people["rows"]
                  if r["wip"] >= args.wip_warn and r["assignee"] != "— не назначен —"]
    if overloaded:
        worst = max(overloaded, key=lambda r: r["wip"])
        sev = "serious" if worst["wip"] >= args.wip_warn * 2 else "warning"
        add(sev, "Перегруз по незавершённым задачам",
            f"{len(overloaded)} чел. держат в работе {args.wip_warn}+ задач "
            f"одновременно (максимум — {worst['wip']} у {worst['assignee']}). "
            "Параллельная работа над многим сразу растягивает время цикла.",
            [{"assignee": r["assignee"], "wip": r["wip"]} for r in overloaded[:10]])

    # Концентрация знаний.
    if people["contributors"] >= 2 and people["bus_factor"] <= 1:
        add("serious", "Низкий bus factor",
            f"Половину закрытых задач сделал один человек "
            f"({people['top_share_pct']}% всего объёма). Уход этого человека "
            "в отпуск или на другой проект резко просадит поток.",
            {"bus_factor": people["bus_factor"], "top_share_pct": people["top_share_pct"]})

    if people["unassigned_open"] > 0:
        sev = "warning" if people["unassigned_open"] >= 5 else "info"
        add(sev, "Есть открытые задачи без исполнителя",
            f"{people['unassigned_open']} открытых задач ни на кого не назначены — "
            "они не попадают ни в чью зону ответственности и легко теряются.",
            {"count": people["unassigned_open"]})

    # Риски.
    if risks["overdue"]:
        add("critical", "Просроченные сроки",
            f"{len(risks['overdue'])} открытых задач с истёкшим due date. "
            "Самые старые — в списке ниже.", risks["overdue"][:10])
    if risks["blocked"]:
        sev = "serious" if len(risks["blocked"]) >= 3 else "warning"
        add(sev, "Заблокированные задачи",
            f"{len(risks['blocked'])} задач помечены как заблокированные или ждут "
            "другие задачи. Блокеры — первое, что стоит разобрать на стендапе.",
            risks["blocked"][:10])
    if risks["aging_wip"]:
        add("warning", "Задачи зависли в работе",
            f"{len(risks['aging_wip'])} задач в работе дольше "
            f"{risks['aging_threshold_days']} дн. (порог — p85 времени цикла). "
            "Обычно это признак скрытого блокера или слишком крупной задачи.",
            risks["aging_wip"][:10])
    if risks["stale"]:
        sev = "warning" if len(risks["stale"]) >= 10 else "info"
        add(sev, "Задачи без движения",
            f"{len(risks['stale'])} открытых задач не обновлялись "
            f"{risks['stale_days']}+ дн. Часть из них, вероятно, стоит закрыть "
            "или вернуть в бэклог.", risks["stale"][:10])
    if risks["long_lived"]:
        add("info", "Долгожители в бэклоге",
            f"{len(risks['long_lived'])} открытых задач старше "
            f"{risks['longlived_days']} дн. Такой хвост искажает оценки сроков.",
            risks["long_lived"][:5])

    # Иерархия: состояние инициатив и целостность связей.
    epics = m.get("epics") or []
    hier = m.get("hierarchy") or {}
    empty = [e for e in epics if "нет ни одной задачи" in e["flags"]]
    if empty:
        add("warning", "Эпики без задач",
            f"{len(empty)} эпиков открыты, но под ними нет ни одной задачи. "
            "Это либо заготовки, которые забыли наполнить, либо контейнеры "
            "«на будущее» — в обоих случаях они создают видимость работы.",
            [{"key": e["key"], "summary": e["summary"]} for e in empty[:10]])
    finished = [e for e in epics if "все задачи сделаны, но эпик открыт" in e["flags"]]
    if finished:
        add("warning", "Эпики можно закрывать",
            f"У {len(finished)} эпиков все задачи выполнены, но сам эпик "
            "остаётся открытым. Быстрая уборка: закрыть и убрать из отчётности.",
            [{"key": e["key"], "summary": e["summary"],
              "children": e["children"]} for e in finished[:10]])
    inconsistent = [e for e in epics if "эпик закрыт, но задачи открыты" in e["flags"]]
    if inconsistent:
        add("serious", "Эпик закрыт, а работа под ним нет",
            f"{len(inconsistent)} эпиков помечены выполненными, хотя под ними "
            "остались незакрытые задачи. Либо работа потеряна из виду, либо "
            "эпик закрыли преждевременно — отчётность по инициативам врёт.",
            [{"key": e["key"], "summary": e["summary"],
              "open_children": e["wip"] + e["todo"]} for e in inconsistent[:10]])
    stuck = [e for e in epics
             if "в работе, но ни одна задача не сделана" in e["flags"]
             and e["children"] >= 3]
    if stuck:
        add("warning", "Инициативы не сдвинулись",
            f"{len(stuck)} эпиков числятся в работе, но ни одна задача под ними "
            "не завершена. Стоит проверить, действительно ли они начаты.",
            [{"key": e["key"], "summary": e["summary"],
              "children": e["children"]} for e in stuck[:10]])
    orphans = hier.get("orphans", 0)
    counted = hier.get("counted_items") or 1
    if epics and orphans and orphans / counted > 0.4:
        add("info", "Много задач вне эпиков",
            f"{orphans} из {counted} задач не привязаны ни к одному эпику "
            f"({round(100.0 * orphans / counted)}%). Команда использует эпики, "
            "но значительная часть работы мимо них — по инициативам не видно "
            "полной картины.",
            {"orphans": orphans, "counted": counted})

    # Качество учёта: без этого метрики описывают Jira, а не работу команды.
    dq = m.get("data_quality", {})
    if dq.get("batch_closures"):
        top = dq["batch_closures"][0]
        total_batched = sum(b["count"] for b in dq["batch_closures"])
        add("serious", "Пачковые закрытия искажают поток",
            f"{total_batched} задач закрыты пачками (например, {top['count']} шт. "
            f"в {top['at']}). Статусы двигают задним числом, поэтому время цикла "
            "и пропускная способность отражают учёт, а не реальную работу. "
            "Метрики станут честными только после того, как команда начнёт "
            "переводить задачи вовремя.",
            dq["batch_closures"][:5])
    if dq.get("instant_count", 0) >= 3:
        add("warning", "Задачи закрываются в момент создания",
            f"{dq['instant_count']} задач созданы и закрыты в течение часа. "
            "Скорее всего, работа шла вне Jira, а задача заведена постфактум — "
            "такие записи занижают время цикла и делают команду быстрее, "
            "чем она есть.", dq.get("instant_closures", [])[:8])

    if risks.get("eternal_wip"):
        add("serious", "Вечные задачи в работе",
            f"{len(risks['eternal_wip'])} задач числятся в работе дольше "
            f"{int(ETERNAL_WIP_DAYS)} дней. Обычно это не работа, а "
            "контейнеры-долгожители (эпики, «разное»): они раздувают WIP и портят "
            "CFD. Их стоит либо разбить на реальные задачи, либо закрыть.",
            risks["eternal_wip"][:10])

    wait = m.get("waiting", {})
    eff = wait.get("flow_efficiency_pct")
    if eff is not None and eff < 60:
        worst = (wait.get("top_issues") or [{}])[0]
        add("warning", "Задачи много времени просто ждут",
            f"Эффективность потока {eff}%: остальное время задачи лежат в "
            "статусах ожидания, а не в работе. "
            + (f"Рекордсмен — {worst.get('key')}: "
               f"{worst.get('waiting_share_pct')}% срока в ожидании. "
               if worst.get("key") else "")
            + "Ускорять тут надо не работу, а передачи и согласования.",
            wait.get("top_issues", [])[:8])

    # Полнота данных — честно про ограничения расчёта.
    if scope["changelog_coverage_pct"] < 100:
        add("info", "Неполная история изменений",
            f"changelog доступен у {scope['changelog_coverage_pct']}% задач. "
            "Время цикла и диаграмма CFD посчитаны только по ним; остальные "
            "оценены по датам создания и решения.",
            {"coverage_pct": scope["changelog_coverage_pct"]})

    order = {"critical": 0, "serious": 1, "warning": 2, "info": 3, "good": 4}
    out.sort(key=lambda w: order.get(w["severity"], 9))
    return out


# --- render: SVG-примитивы ---------------------------------------------------
# Графики рисуем инлайновым SVG без библиотек: файл должен открываться в
# корпоративной сети без интернета. Цвета берём из CSS-переменных, поэтому
# светлая и тёмная темы переключаются в одном месте.

def esc(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _nice_scale(v: float) -> tuple[float, float]:
    """Максимум оси и шаг деления, чтобы подписи были круглыми числами.
    Ось всегда делится на 4 интервала — иначе получаются деления вида 0,4,8,11,15."""
    if v <= 0:
        return 4.0, 1.0
    raw = v / 4.0
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if step >= raw - 1e-9:
            return step * 4, step
    return raw * 4, raw


def _nice_max(v: float) -> float:
    return _nice_scale(v)[0]


def _xlabels(labels: list[str], max_n: int = 8) -> list[int]:
    """Индексы подписей оси X, чтобы они не сталкивались."""
    if len(labels) <= max_n:
        return list(range(len(labels)))
    step = math.ceil(len(labels) / max_n)
    idx = list(range(0, len(labels), step))
    last = len(labels) - 1
    if idx[-1] != last:
        # Последнюю подпись показываем всегда, но если она слипается с
        # предыдущей — предыдущую убираем, иначе тексты наезжают друг на друга.
        if last - idx[-1] < step * 0.6:
            idx.pop()
        idx.append(last)
    return idx


def _fmt(v: float) -> str:
    return str(int(round(v))) if abs(v - round(v)) < 1e-9 else f"{v:g}"


def _axes(w: int, h: int, ml: int, mr: int, mt: int, mb: int,
          ymax: float, labels: list[str], step: float | None = None) -> str:
    """Сетка, ось Y с делениями и подписи оси X. Оформление рецессивное."""
    parts = []
    plot_h = h - mt - mb
    step = step if step else ymax / 4.0
    for t in range(5):
        val = step * t
        y = mt + plot_h - plot_h * t / 4
        parts.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{w - mr}" y2="{y:.1f}" '
            f'stroke="var(--grid)" stroke-width="1"/>')
        parts.append(
            f'<text x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'class="ax">{_fmt(val)}</text>')
    parts.append(f'<line x1="{ml}" y1="{mt + plot_h}" x2="{w - mr}" '
                 f'y2="{mt + plot_h}" stroke="var(--baseline)" stroke-width="1"/>')
    n = max(len(labels), 1)
    step = (w - ml - mr) / n
    for i in _xlabels(labels):
        x = ml + step * (i + 0.5)
        parts.append(f'<text x="{x:.1f}" y="{h - mb + 16}" text-anchor="middle" '
                     f'class="ax">{esc(labels[i])}</text>')
    return "".join(parts)


def svg_lines(labels: list[str], series: list[dict], unit: str = "") -> str:
    """Линейный график: 2px линии, маркеры >=8px, прямая подпись последней точки."""
    w, h, ml, mr, mt, mb = 860, 300, 46, 96, 16, 34
    allv = [v for s in series for v in s["values"]] or [0]
    ymax, ystep = _nice_scale(max(allv))
    plot_w, plot_h = w - ml - mr, h - mt - mb
    n = max(len(labels), 1)
    step = plot_w / n
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" preserveAspectRatio="xMidYMid meet">']
    out.append(_axes(w, h, ml, mr, mt, mb, ymax, labels, ystep))
    for s in series:
        pts = []
        for i, v in enumerate(s["values"]):
            x = ml + step * (i + 0.5)
            y = mt + plot_h - (v / ymax) * plot_h if ymax else mt + plot_h
            pts.append((x, y, v))
        d = " ".join(f'{"M" if k == 0 else "L"}{x:.1f},{y:.1f}'
                     for k, (x, y, _) in enumerate(pts))
        out.append(f'<path d="{d}" fill="none" stroke="{s["color"]}" '
                   f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        for x, y, v in pts:
            out.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{s["color"]}" '
                f'stroke="var(--surface)" stroke-width="2">'
                f'<title>{esc(s["name"])}: {v}{esc(unit)}</title></circle>')
        if pts:
            lx, ly, lv = pts[-1]
            out.append(f'<text x="{lx + 10:.1f}" y="{ly + 4:.1f}" class="lbl" '
                       f'fill="{s["color"]}">{esc(s["name"])} {lv}</text>')
    out.append("</svg>")
    return "".join(out)


def svg_bars(labels: list[str], values: list[float], color: str,
             unit: str = "") -> str:
    """Столбики: скруглённые концы у данных, 2px зазор фоном между соседями."""
    w, h, ml, mr, mt, mb = 860, 260, 46, 16, 16, 34
    ymax, ystep = _nice_scale(max(values or [0]))
    plot_w, plot_h = w - ml - mr, h - mt - mb
    n = max(len(values), 1)
    step = plot_w / n
    bw = max(step - 4, 2)
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" preserveAspectRatio="xMidYMid meet">']
    out.append(_axes(w, h, ml, mr, mt, mb, ymax, labels, ystep))
    for i, v in enumerate(values):
        bh = (v / ymax) * plot_h if ymax else 0
        x = ml + step * i + (step - bw) / 2
        y = mt + plot_h - bh
        r = min(4, bw / 2, max(bh, 0.1))
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                   f'height="{max(bh, 0):.1f}" rx="{r:.1f}" fill="{color}">'
                   f'<title>{esc(labels[i] if i < len(labels) else i)}: '
                   f'{v}{esc(unit)}</title></rect>')
    out.append("</svg>")
    return "".join(out)


def svg_stacked_area(labels: list[str], stacks: list[dict]) -> str:
    """Накопительная диаграмма потока (CFD). Сегменты разделены линией цвета
    подложки — это тот самый 2px зазор, который не даёт слипнуться заливкам."""
    w, h, ml, mr, mt, mb = 860, 300, 46, 96, 16, 34
    n = max(len(labels), 1)
    totals = [sum(s["values"][i] for s in stacks) for i in range(n)]
    ymax, ystep = _nice_scale(max(totals or [0]))
    plot_w, plot_h = w - ml - mr, h - mt - mb
    step = plot_w / n
    xs = [ml + step * (i + 0.5) for i in range(n)]
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" preserveAspectRatio="xMidYMid meet">']
    out.append(_axes(w, h, ml, mr, mt, mb, ymax, labels, ystep))
    base = [0.0] * n
    labels_out: list[tuple[float, str, str]] = []
    for s in stacks:
        top = [base[i] + s["values"][i] for i in range(n)]
        def y_of(v):
            return mt + plot_h - (v / ymax) * plot_h if ymax else mt + plot_h
        up = " ".join(f'{"M" if k == 0 else "L"}{xs[k]:.1f},{y_of(top[k]):.1f}'
                      for k in range(n))
        down = " ".join(f'L{xs[k]:.1f},{y_of(base[k]):.1f}' for k in range(n - 1, -1, -1))
        out.append(f'<path d="{up} {down} Z" fill="{s["color"]}" fill-opacity="0.85"/>')
        out.append(f'<path d="{up}" fill="none" stroke="var(--surface)" '
                   f'stroke-width="2"/>')
        if n:
            labels_out.append((y_of(top[-1]) + 4, s["name"], s["color"]))
        base = top
    # Подписи разводим ПОСЛЕ отрисовки, сверху вниз: так они не наезжают друг на
    # друга и сохраняют тот же порядок, что и полосы на графике.
    prev = -1e9
    for ly, name, color in sorted(labels_out, key=lambda t: t[0]):
        ly = max(ly, prev + 15)
        prev = ly
        out.append(f'<text x="{xs[-1] + 10:.1f}" y="{ly:.1f}" '
                   f'class="lbl" fill="{color}">{esc(name)}</text>')
    out.append("</svg>")
    return "".join(out)


def svg_hbars(rows: list[dict], series: list[dict], unit: str = "") -> str:
    """Горизонтальные группированные столбики — распределение по людям."""
    if not rows:
        return '<p class="empty">Нет данных</p>'
    # Отступ слева считаем по самой длинной подписи, иначе имена обрезаются
    # («ИО-182» вместо «DEMO-182»). Ограничиваем сверху, чтобы график не съёжился.
    labels = [str(r["label"]) for r in rows]
    longest = max((len(x) for x in labels), default=10)
    row_h, gap, mr, mt = 30, 10, 70, 8
    ml = int(min(320, max(120, longest * 6.6 + 14)))
    h = mt + len(rows) * (row_h + gap)
    w = 860
    maxv = _nice_scale(max([r[s["field"]] for r in rows for s in series] or [0]))[0]
    plot_w = w - ml - mr
    bh = (row_h - 4) / max(len(series), 1)
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" preserveAspectRatio="xMidYMid meet">']
    for i, r in enumerate(rows):
        y0 = mt + i * (row_h + gap)
        out.append(f'<text x="{ml - 10}" y="{y0 + row_h / 2 + 4:.1f}" '
                   f'text-anchor="end" class="lbl">{esc(labels[i])}</text>')
        for j, s in enumerate(series):
            v = r[s["field"]]
            bw = (v / maxv) * plot_w if maxv else 0
            y = y0 + j * bh + 1
            rr = min(4, bh / 2, max(bw, 0.1))
            out.append(f'<rect x="{ml}" y="{y:.1f}" width="{max(bw, 0):.1f}" '
                       f'height="{bh - 2:.1f}" rx="{rr:.1f}" fill="{s["color"]}">'
                       f'<title>{esc(r["label"])} — {esc(s["name"])}: {v}{esc(unit)}'
                       f'</title></rect>')
            if v:
                out.append(f'<text x="{ml + bw + 6:.1f}" y="{y + bh / 2 + 3:.1f}" '
                           f'class="ax">{v}</text>')
    out.append("</svg>")
    return "".join(out)


def svg_histogram(values: list[float], marks: dict[str, float | None],
                  color: str) -> str:
    """Распределение времени с отметками перцентилей — «хвост» виден глазом."""
    if not values:
        return '<p class="empty">Недостаточно данных (нужен changelog)</p>'
    w, h, ml, mr, mt, mb = 860, 260, 46, 16, 24, 40
    vmax = max(values)
    nb = min(16, max(6, int(math.sqrt(len(values))) + 2))
    width = (vmax or 1) / nb
    counts = [0] * nb
    for v in values:
        counts[min(nb - 1, int(v / width) if width else 0)] += 1
    labels = [f"{i * width:.0f}" for i in range(nb)]
    ymax, ystep = _nice_scale(max(counts))
    plot_w, plot_h = w - ml - mr, h - mt - mb
    step = plot_w / nb
    bw = max(step - 4, 2)
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" preserveAspectRatio="xMidYMid meet">']
    out.append(_axes(w, h, ml, mr, mt, mb, ymax, labels, ystep))
    for i, c in enumerate(counts):
        bh = (c / ymax) * plot_h if ymax else 0
        x = ml + step * i + (step - bw) / 2
        y = mt + plot_h - bh
        r = min(4, bw / 2, max(bh, 0.1))
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                   f'height="{max(bh, 0):.1f}" rx="{r:.1f}" fill="{color}">'
                   f'<title>{i * width:.0f}–{(i + 1) * width:.0f} дн.: {c} задач</title>'
                   f'</rect>')
    for name, val in marks.items():
        if val is None or not vmax:
            continue
        x = ml + (min(val, vmax) / vmax) * plot_w
        out.append(f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{mt + plot_h}" '
                   f'stroke="var(--text-secondary)" stroke-width="1" '
                   f'stroke-dasharray="4 3"/>')
        out.append(f'<text x="{x:.1f}" y="{mt - 8}" text-anchor="middle" '
                   f'class="ax">{esc(name)} {val:g}д</text>')
    out.append("</svg>")
    return "".join(out)


# --- render: сборка страницы -------------------------------------------------

# Палитра проверена валидатором dataviz в обеих темах (categorical, adjacent).
CSS = """
:root{color-scheme:light;
--surface:#fcfcfb;--plane:#f9f9f7;--text:#0b0b0b;--text-secondary:#52514e;
--muted:#898781;--grid:#e1e0d9;--baseline:#c3c2b7;--border:#e1e0d9;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;
--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
--surface:#1a1a19;--plane:#0d0d0d;--text:#fff;--text-secondary:#c3c2b7;
--muted:#898781;--grid:#2c2c2a;--baseline:#383835;--border:#2c2c2a;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;}}
:root[data-theme="dark"]{color-scheme:dark;
--surface:#1a1a19;--plane:#0d0d0d;--text:#fff;--text-secondary:#c3c2b7;
--muted:#898781;--grid:#2c2c2a;--baseline:#383835;--border:#2c2c2a;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--text);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:17px;margin:34px 0 12px;padding-top:18px;border-top:1px solid var(--border)}
h3{font-size:14px;margin:20px 0 8px;color:var(--text-secondary);font-weight:600}
.sub{color:var(--text-secondary);margin:0 0 20px;font-size:13px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
padding:16px;margin:12px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.tile .v{font-size:24px;font-weight:650;letter-spacing:-.02em}
.tile .k{font-size:12px;color:var(--text-secondary);margin-top:2px}
.tile .h{font-size:11px;color:var(--muted);margin-top:4px}
svg{width:100%;height:auto;display:block}
text.ax{font-size:11px;fill:var(--muted)}
text.lbl{font-size:12px;fill:var(--text-secondary)}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:2px 0 10px;font-size:12px;
color:var(--text-secondary)}
.legend i{width:10px;height:10px;border-radius:3px;display:inline-block;margin-right:6px}
.w{border-left:3px solid var(--muted);background:var(--surface);border-radius:0 8px 8px 0;
padding:12px 14px;margin:8px 0;border-top:1px solid var(--border);
border-right:1px solid var(--border);border-bottom:1px solid var(--border)}
.w .t{font-weight:650;display:flex;align-items:center;gap:8px}
.w .d{color:var(--text-secondary);margin-top:4px;font-size:13px}
.w.critical{border-left-color:var(--critical)}.w.serious{border-left-color:var(--serious)}
.w.warning{border-left-color:var(--warning)}.w.info{border-left-color:var(--s1)}
.w.good{border-left-color:var(--good)}
.badge{font-size:11px;font-weight:600;padding:2px 7px;border-radius:20px;
border:1px solid var(--border);color:var(--text-secondary);white-space:nowrap}
table{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border)}
th{color:var(--text-secondary);font-weight:600;font-size:12px}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
details{margin-top:10px}summary{cursor:pointer;color:var(--text-secondary);font-size:12px}
.empty{color:var(--muted);font-size:13px;margin:8px 0}
code{background:var(--plane);padding:1px 5px;border-radius:4px;font-size:12px}
.summary{font-size:15px;line-height:1.6;border-left:3px solid var(--s1)}
.keys{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
.w .d b{color:var(--text);font-weight:600}
ol{margin:6px 0 0 18px;padding:0}ol li{margin:4px 0}
.foot{color:var(--muted);font-size:12px;margin-top:34px;border-top:1px solid var(--border);
padding-top:14px}
"""

SEV = {
    "critical": ("Критично", "critical", "△"),
    "serious":  ("Серьёзно", "serious", "△"),
    "warning":  ("Внимание", "warning", "△"),
    "info":     ("Инфо", "info", "○"),
    "good":     ("Хорошо", "good", "✓"),
}


def _tile(value: Any, key: str, hint: str = "") -> str:
    v = "—" if value is None else value
    h = f'<div class="h">{esc(hint)}</div>' if hint else ""
    return (f'<div class="tile"><div class="v">{esc(v)}</div>'
            f'<div class="k">{esc(key)}</div>{h}</div>')


def _table(headers: list[str], rows: list[list[Any]], numeric: set[int] | None = None,
           empty: str = "Ничего не найдено") -> str:
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    numeric = numeric or set()
    th = "".join(f'<th class="{"n" if i in numeric else ""}">{esc(h)}</th>'
                 for i, h in enumerate(headers))
    trs = []
    for r in rows:
        tds = "".join(f'<td class="{"n" if i in numeric else ""}">{esc(c)}</td>'
                      for i, c in enumerate(r))
        trs.append(f"<tr>{tds}</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>"


def _legend(items: list[tuple[str, str]]) -> str:
    return ('<div class="legend">' + "".join(
        f'<span><i style="background:{c}"></i>{esc(n)}</span>' for n, c in items)
        + "</div>")


def _details(summary: str, body: str) -> str:
    return f"<details><summary>{esc(summary)}</summary>{body}</details>"


def render_html(m: dict, title: str) -> str:
    flow, people, risks, scope = m["flow"], m["people"], m["risks"], m["scope"]
    lead, cycle = flow["lead_time"], flow["cycle_time"]
    per = m["period"]
    gran_ru = {"day": "по дням", "week": "по неделям", "month": "по месяцам"}[
        per["granularity"]]

    h: list[str] = []
    h.append(f"<h1>{esc(title)}</h1>")
    h.append(f'<p class="sub">Период: {esc(per["from"][:10])} — {esc(per["to"][:10])} '
             f'({esc(gran_ru)}) · задач в выборке: {scope["total_issues"]} · '
             f'отчёт сформирован {esc(m["generated_at"][:16].replace("T", " "))}</p>')

    # KPI
    h.append('<div class="tiles">')
    h.append(_tile(scope["created_in_period"], "Создано за период"))
    h.append(_tile(scope["resolved_in_period"], "Закрыто за период"))
    h.append(_tile(flow["throughput_avg"], f"Пропускная способность", gran_ru))
    h.append(_tile(scope["open_now"], "Открыто сейчас"))
    h.append(_tile(lead["p50"], "Lead time, медиана", "дней, создание → закрытие"))
    h.append(_tile(lead["p85"], "Lead time, p85", "дней, «почти худший» случай"))
    h.append(_tile(cycle["p50"], "Cycle time, медиана", "дней в работе"))
    h.append(_tile(people["bus_factor"], "Bus factor",
                   f'{people["contributors"]} участников'))
    h.append("</div>")

    # Анализ ИИ — впереди всего: это интерпретация, ради которой отчёт читают.
    ai = m.get("ai_insights") or {}
    if ai:
        h.append("<h2>Анализ: что происходит и что делать</h2>")
        if ai.get("executive_summary"):
            h.append(f'<div class="card summary">{esc(ai["executive_summary"])}'
                     f'</div>')
        for f in ai.get("findings", []):
            label, cls, icon = SEV.get(f.get("severity"), ("Инфо", "info", "○"))
            conf = {"high": "уверенно", "medium": "вероятно",
                    "low": "гипотеза"}.get(f.get("confidence"), "")
            h.append(f'<div class="w {cls}"><div class="t">{icon} '
                     f'{esc(f.get("title", ""))}'
                     f'<span class="badge">{esc(label)}</span>'
                     + (f'<span class="badge">{esc(conf)}</span>' if conf else "")
                     + '</div>')
            for field, prefix in (("observation", "Что видно"),
                                  ("interpretation", "Что это значит"),
                                  ("recommendation", "Что делать")):
                if f.get(field):
                    h.append(f'<div class="d"><b>{prefix}:</b> '
                             f'{esc(f[field])}</div>')
            keys = f.get("evidence_keys") or []
            if keys:
                h.append('<div class="keys">' + " ".join(
                    f"<code>{esc(k)}</code>" for k in keys[:12]) + "</div>")
            h.append("</div>")
        if ai.get("next_steps"):
            h.append("<h3>Следующие шаги</h3><div class=\"card\"><ol>" + "".join(
                f"<li>{esc(x)}</li>" for x in ai["next_steps"]) + "</ol></div>")
        h.append('<p class="sub">Раздел выше — интерпретация модели по фактам '
                 'из расчёта; каждая находка сослана на конкретные задачи. '
                 'Ниже — числа и правила, посчитанные детерминированно.</p>')

    # Предупреждения
    h.append("<h2>Расчётные предупреждения</h2>")
    if not m["warnings"]:
        h.append('<p class="empty">Правила не нашли отклонений.</p>')
    for w in m["warnings"]:
        label, cls, icon = SEV.get(w["severity"], ("Инфо", "info", "○"))
        h.append(f'<div class="w {cls}"><div class="t">{icon} {esc(w["title"])}'
                 f'<span class="badge">{esc(label)}</span></div>'
                 f'<div class="d">{esc(w["detail"])}</div>')
        ev = w.get("evidence")
        if isinstance(ev, list) and ev and isinstance(ev[0], dict) and "key" in ev[0]:
            rows = [[e.get("key"), e.get("summary", ""), e.get("assignee") or "—",
                     next((v for k, v in e.items()
                           if k.endswith(("_days", "_idle"))), "")]
                    for e in ev]
            h.append(_details(f"Задачи ({len(ev)})",
                              _table(["Ключ", "Тема", "Исполнитель", "Дней"],
                                     rows, {3})))
        h.append("</div>")

    # Поток
    h.append("<h2>Поток и скорость</h2>")
    h.append("<h3>Создано против закрытого</h3>")
    h.append('<div class="card">')
    h.append(_legend([("Создано", "var(--s1)"), ("Закрыто", "var(--s2)")]))
    h.append(svg_lines(flow["buckets"], [
        {"name": "Создано", "values": flow["created"], "color": "var(--s1)"},
        {"name": "Закрыто", "values": flow["resolved"], "color": "var(--s2)"}]))
    h.append(_details("Показать таблицей", _table(
        ["Период", "Создано", "Закрыто", "Дельта"],
        [[b, c, r, f"{n:+d}"] for b, c, r, n in zip(
            flow["buckets"], flow["created"], flow["resolved"], flow["net"])],
        {1, 2, 3})))
    h.append("</div>")

    h.append("<h3>Пропускная способность</h3>")
    h.append('<div class="card">')
    h.append(svg_bars(flow["buckets"], flow["resolved"], "var(--s1)", " задач"))
    h.append("</div>")

    h.append("<h3>Накопительная диаграмма потока (CFD)</h3>")
    h.append('<div class="card">')
    h.append(_legend([(CATEGORY_RU[TODO], "var(--s1)"),
                      (CATEGORY_RU[WIP], "var(--s2)"),
                      (CATEGORY_RU[DONE], "var(--s3)")]))
    h.append(svg_stacked_area(flow["buckets"], [
        {"name": CATEGORY_RU[DONE], "values": flow["cfd"][DONE], "color": "var(--s3)"},
        {"name": CATEGORY_RU[WIP], "values": flow["cfd"][WIP], "color": "var(--s2)"},
        {"name": CATEGORY_RU[TODO], "values": flow["cfd"][TODO], "color": "var(--s1)"}]))
    h.append('<p class="sub" style="margin:8px 0 0">Расширяющаяся оранжевая полоса — '
             'растущая незавершёнка: работу начинают быстрее, чем заканчивают.</p>')
    h.append("</div>")

    h.append("<h3>Распределение времени цикла</h3>")
    h.append('<div class="card">')
    if cycle["count"]:
        # Гистограмму строим по перцентилям, восстановленным из метрик.
        h.append(svg_histogram(m.get("_cycle_values", []) or [],
                               {"p50": cycle["p50"], "p85": cycle["p85"]},
                               "var(--s1)"))
    else:
        h.append('<p class="empty">Время цикла посчитать не удалось: у задач нет '
                 'истории изменений (changelog). Выгрузите задачи с '
                 '<code>expand=changelog</code>.</p>')
    h.append(_table(["Метрика", "p50", "p75", "p85", "p95", "Максимум"],
                    [["Lead time (создание → закрытие), дн.", lead["p50"], lead["p75"],
                      lead["p85"], lead["p95"], lead["max"]],
                     ["Cycle time (в работе → закрытие), дн.", cycle["p50"],
                      cycle["p75"], cycle["p85"], cycle["p95"], cycle["max"]]],
                    {1, 2, 3, 4, 5}))
    h.append("</div>")

    # Люди
    h.append("<h2>Люди и нагрузка</h2>")
    h.append('<div class="card">')
    h.append(_legend([("Закрыто за период", "var(--s1)"),
                      ("В работе сейчас", "var(--s2)")]))
    rows = [{"label": r["assignee"], "resolved": r["resolved"], "wip": r["wip"]}
            for r in people["rows"][:12]]
    h.append(svg_hbars(rows, [
        {"name": "Закрыто", "field": "resolved", "color": "var(--s1)"},
        {"name": "В работе", "field": "wip", "color": "var(--s2)"}]))
    h.append(_table(
        ["Исполнитель", "Закрыто", "В работе", "Открыто", "Cycle p50, дн."],
        [[r["assignee"], r["resolved"], r["wip"], r["open"],
          r["cycle_p50"] if r["cycle_p50"] is not None else "—"]
         for r in people["rows"]], {1, 2, 3, 4}))
    h.append(f'<p class="sub" style="margin:10px 0 0">Участников с закрытыми задачами: '
             f'{people["contributors"]} · доля лидера: {people["top_share_pct"]}% · '
             f'открытых без исполнителя: {people["unassigned_open"]}</p>')
    h.append("</div>")

    # Эпики
    epics = m.get("epics") or []
    hier = m.get("hierarchy") or {}
    if epics:
        h.append("<h2>Эпики: прогресс и затыки</h2>")
        h.append('<div class="card">')
        rows = [{"label": f'{e["key"]} · {e["summary"][:30]}',
                 "done": e["done"], "left": e["wip"] + e["todo"]}
                for e in epics[:12] if e["children"]]
        if rows:
            h.append(_legend([("Сделано", "var(--s3)"), ("Осталось", "var(--s2)")]))
            h.append(svg_hbars(rows, [
                {"name": "Сделано", "field": "done", "color": "var(--s3)"},
                {"name": "Осталось", "field": "left", "color": "var(--s2)"}]))
        h.append(_table(
            ["Эпик", "Тема", "Задач", "Готово", "%", "В работе", "Блок.",
             "Простой, дн.", "Замечания"],
            [[e["key"], e["summary"], e["children"], e["done"], e["progress_pct"],
              e["wip"], e["blocked"],
              e["idle_days"] if e["idle_days"] is not None else "—",
              "; ".join(e["flags"]) or "—"] for e in epics[:25]],
            {2, 3, 4, 5, 6, 7}))
        h.append(f'<p class="sub" style="margin:10px 0 0">Эпиков: '
                 f'{hier.get("epics_total", 0)} · подзадач: '
                 f'{hier.get("subtasks", 0)} · вне эпиков: '
                 f'{hier.get("orphans", 0)}. '
                 + ("Эпики исключены из метрик потока как контейнеры: они не "
                    "единица работы и искажали бы объём и время цикла."
                    if hier.get("scope_items") == "leaf"
                    else "Эпики учтены в метриках потока наравне с задачами.")
                 + "</p>")
        h.append("</div>")

    # Ожидание
    wait = m.get("waiting") or {}
    if wait.get("by_status_days"):
        h.append("<h2>Где время утекает</h2>")
        h.append('<div class="card">')
        eff = wait.get("flow_efficiency_pct")
        if eff is not None:
            h.append(_tile(f"{eff}%", "Эффективность потока",
                           "доля времени в работе, а не в ожидании"))
        rows = [{"label": x["status"], "days": x["days"]}
                for x in wait["by_status_days"][:10] if x["days"] > 0]
        h.append(svg_hbars(rows, [{"name": "Дней суммарно", "field": "days",
                                   "color": "var(--s1)"}], " дн."))
        if wait.get("top_issues"):
            h.append("<h3>Дольше всех ждут</h3>")
            h.append(_table(["Ключ", "Тема", "В ожидании, дн.", "Доля срока, %"],
                            [[x["key"], x["summary"], x["waiting_days"],
                              x["waiting_share_pct"]]
                             for x in wait["top_issues"][:15]], {2, 3}))
        h.append("</div>")

    # Качество учёта
    dq = m.get("data_quality") or {}
    if dq.get("batch_closures") or dq.get("instant_count"):
        h.append("<h2>Качество учёта</h2>")
        h.append('<div class="card">')
        h.append('<p class="sub" style="margin-top:0">Эти сигналы говорят не о '
                 'команде, а о том, насколько Jira отражает реальную работу. '
                 'Если их много, метрики времени стоит трактовать осторожно.</p>')
        if dq.get("batch_closures"):
            h.append("<h3>Пачковые закрытия</h3>")
            h.append(_table(["Когда", "Закрыто за час", "Задачи"],
                            [[b["at"], b["count"], ", ".join(b["keys"][:8])]
                             for b in dq["batch_closures"]], {1}))
        if dq.get("instant_closures"):
            h.append("<h3>Закрыты в момент создания</h3>")
            h.append(_table(["Ключ", "Тема", "Минут от создания"],
                            [[x["key"], x["summary"], x["minutes"]]
                             for x in dq["instant_closures"][:15]], {2}))
        h.append("</div>")

    # Риски
    h.append("<h2>Риски и блокеры</h2>")
    h.append('<div class="tiles">')
    h.append(_tile(len(risks["blocked"]), "Заблокировано"))
    h.append(_tile(len(risks.get("eternal_wip", [])), "Вечные в работе",
                   f"дольше {int(ETERNAL_WIP_DAYS)} дн."))
    h.append(_tile(len(risks["aging_wip"]), "Зависли в работе",
                   f'дольше {risks["aging_threshold_days"]} дн.'))
    h.append(_tile(len(risks["stale"]), "Без движения",
                   f'{risks["stale_days"]}+ дн.'))
    h.append(_tile(len(risks["overdue"]), "Просрочено"))
    h.append("</div>")
    for key, title_, cols in (
        ("eternal_wip", "Вечные задачи в работе", "in_progress_days"),
        ("overdue", "Просроченные", "overdue_days"),
        ("blocked", "Заблокированные", None),
        ("aging_wip", "Зависли в работе", "in_progress_days"),
        ("stale", "Без движения", "days_idle"),
    ):
        items = risks[key]
        if not items:
            continue
        head = ["Ключ", "Тема", "Статус", "Исполнитель"]
        body = [[i["key"], i["summary"], i["status"], i.get("assignee") or "—"]
                for i in items]
        if cols:
            head.append("Дней")
            for row, i in zip(body, items):
                row.append(i.get(cols))
        h.append(f"<h3>{esc(title_)} ({len(items)})</h3>")
        h.append('<div class="card">' +
                 _table(head, body[:25], {4} if cols else set()) +
                 (f'<p class="sub" style="margin:8px 0 0">Показаны первые 25 из '
                  f'{len(items)}.</p>' if len(items) > 25 else "") + "</div>")

    h.append('<div class="foot">Данные: Jira (Server/Data Center). Расчёт выполнен '
             f'локально, без передачи данных наружу. Покрытие историей изменений: '
             f'{scope["changelog_coverage_pct"]}% задач — от него зависит точность '
             'времени цикла и CFD. Пороги: «без движения» — '
             f'{risks["stale_days"]} дн., «зависло в работе» — '
             f'{risks["aging_threshold_days"]} дн. (p85 времени цикла).</div>')

    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title><style>{CSS}</style></head>'
            f'<body><div class="wrap">{"".join(h)}</div></body></html>')


def cmd_render(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.input):
        return _emit({"error": f"файл не найден: {args.input}"}, ok=False)
    with open(args.input, encoding="utf-8") as fh:
        m = json.load(fh)
    if args.issues and os.path.isfile(args.issues):
        # Значения времени цикла нужны для гистограммы — восстанавливаем из задач.
        with open(args.issues, encoding="utf-8") as fh:
            issues = json.load(fh)
        status_cat = {i["status"]: i["status_category"] for i in issues if i.get("status")}
        wip_statuses = {st for st, c in status_cat.items() if c == WIP}
        done_statuses = {st for st, c in status_cat.items() if c == DONE}
        vals = []
        for i in issues:
            r = parse_dt(i.get("resolved"))
            w = first_wip_time(i, wip_statuses, done_statuses)[0]
            if r and w and r >= w:
                vals.append(days_between(w, r))
        m["_cycle_values"] = vals
    if args.insights:
        if not os.path.isfile(args.insights):
            return _emit({"error": f"файл не найден: {args.insights}"}, ok=False)
        with open(args.insights, encoding="utf-8") as fh:
            m["ai_insights"] = json.load(fh)
    html = render_html(m, args.title)
    _atomic_write(args.out, html)
    return _emit({
        "output": os.path.abspath(args.out),
        "bytes": os.path.getsize(args.out),
        "self_contained": True,
        "warnings": len(m.get("warnings", [])),
        "ai_findings": len((m.get("ai_insights") or {}).get("findings", [])),
    }, ok=True)


# --- demo --------------------------------------------------------------------


def cmd_demo(args: argparse.Namespace) -> int:
    """Синтетическая выгрузка в формате Jira DC REST — чтобы проверить весь
    конвейер (normalize → analyze → render) без доступа к Jira."""
    import random
    rnd = random.Random(args.seed)
    people = ["Иван Петров", "Мария Соколова", "Алексей Крылов", "Ольга Титова", None]
    types = ["Task", "Bug", "Story"]
    statuses = [("Open", TODO), ("In Progress", WIP), ("Blocked", WIP),
                ("In Review", WIP), ("Done", DONE)]

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.days)
    # Данные намеренно «грязные», как в живой Jira: ожидания, пачковые
    # закрытия, мгновенные закрытия и вечные эпики. Иначе детекторам качества
    # учёта нечего ловить и их нельзя проверить.
    batch_moments = [now - timedelta(days=d, hours=3) for d in (33, 5)]
    issues = []
    for n in range(args.count):
        created = start + timedelta(days=rnd.uniform(0, args.days * 0.92),
                                    hours=rnd.uniform(0, 23))
        # Часть задач закрыта; хвост намеренно длинный, чтобы p95 отличался.
        closed = rnd.random() < 0.68
        assignee = rnd.choice(people)
        cl, resolved, status, cat = [], None, None, None
        wip_at = created + timedelta(days=rnd.uniform(0.2, 6))
        if wip_at < now:
            cl.append({"at": iso(wip_at), "from": "Open", "to": "In Progress"})
        # Часть задач надолго уходит в ожидание уточнений.
        wait_days = 0.0
        if wip_at < now and rnd.random() < 0.22:
            w0 = wip_at + timedelta(days=rnd.uniform(0.5, 3))
            wait_days = rnd.uniform(3, 45)
            w1 = w0 + timedelta(days=wait_days)
            if w1 < now:
                cl.append({"at": iso(w0), "from": "In Progress", "to": "Need Info"})
                cl.append({"at": iso(w1), "from": "Need Info", "to": "In Progress"})
            else:
                wait_days = 0.0
        if closed and wip_at < now:
            dur = rnd.choice([rnd.uniform(0.5, 4), rnd.uniform(4, 12),
                              rnd.uniform(12, 40)])
            r = wip_at + timedelta(days=dur + wait_days)
            # Каждая пятая закрывается «пачкой» — статус двигают задним числом.
            if rnd.random() < 0.2:
                r = rnd.choice(batch_moments) + timedelta(minutes=rnd.uniform(0, 55))
            if r < now and r > wip_at:
                resolved, status, cat = r, "Done", DONE
                cl.append({"at": iso(r), "from": "In Progress", "to": "Done"})
        # Несколько задач заводят постфактум и закрывают сразу.
        if not resolved and rnd.random() < 0.04:
            r = created + timedelta(minutes=rnd.uniform(1, 40))
            if r < now:
                resolved, status, cat = r, "Done", DONE
                cl = [{"at": iso(r), "from": "Open", "to": "Done"}]
        if not resolved:
            status, cat = rnd.choice(statuses[:4])
            if wip_at >= now:
                status, cat = "Open", TODO
        updated = resolved or (created + timedelta(days=rnd.uniform(0, args.days * 0.6)))
        updated = min(updated, now)
        fields = {
            "summary": f"Демо-задача {n + 1}: {rnd.choice(['доработка формы', 'ошибка в отчёте', 'интеграция API', 'рефакторинг', 'настройка прав'])}",
            "issuetype": {"name": rnd.choice(types)},
            "status": {"name": status, "statusCategory": {"key": cat}},
            "priority": {"name": rnd.choice(["High", "Medium", "Low"])},
            "assignee": {"displayName": assignee} if assignee else None,
            "reporter": {"displayName": rnd.choice(people[:4])},
            "created": created.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            "resolutiondate": resolved.strftime("%Y-%m-%dT%H:%M:%S.000+0000") if resolved else None,
            "updated": updated.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            "duedate": ((created + timedelta(days=rnd.uniform(5, 40))).strftime("%Y-%m-%d")
                        if rnd.random() < 0.3 else None),
            "labels": rnd.sample(["frontend", "backend", "infra", "ux"],
                                 k=rnd.randint(0, 2)),
            "components": [{"name": rnd.choice(["Портал", "Бэкофис", "Платежи"])}],
            "customfield_10002": rnd.choice([1, 2, 3, 5, 8, None]),
            "issuelinks": ([{"type": {"inward": "is blocked by"},
                             "inwardIssue": {"key": f"DEMO-{rnd.randint(1, args.count)}"}}]
                           if rnd.random() < 0.06 else []),
        }
        if rnd.random() < 0.05:
            fields["customfield_10100"] = {"value": "Impediment"}
        # Привязка к эпикам и подзадачи — чтобы иерархия была не пустой.
        if 3 <= n < 120:
            fields["customfield_10014"] = f"DEMO-{rnd.choice([1, 2, 3, 181, 182])}"
            if rnd.random() < 0.15:
                fields["issuetype"] = {"name": "Sub-task", "subtask": True}
                fields["parent"] = {"key": f"DEMO-{rnd.randint(4, 100)}"}
                fields.pop("customfield_10014", None)
        # Вечные эпики-контейнеры: живут в работе годами.
        if n < 3:
            created = now - timedelta(days=rnd.uniform(300, 600))
            wip_at = created + timedelta(days=2)
            resolved, status, cat = None, "In Progress", WIP
            cl = [{"at": iso(wip_at), "from": "Open", "to": "In Progress"}]
            fields["issuetype"] = {"name": "Epic"}
            fields["summary"] = rnd.choice(
                ["Расчёт эффекта (контейнер)", "A/B тесты — общее",
                 "Ad-hoc задачи квартала"])
            fields["created"] = created.strftime("%Y-%m-%dT%H:%M:%S.000+0000")
            fields["resolutiondate"] = None
            fields["status"] = {"name": status, "statusCategory": {"key": cat}}
            fields["updated"] = (now - timedelta(days=rnd.uniform(20, 60))
                                 ).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        issues.append({"key": f"DEMO-{n + 1}", "fields": fields,
                       "changelog": {"histories": [
                           {"created": e["at"],
                            "items": [{"field": "status", "fromString": e["from"],
                                       "toString": e["to"]}]} for e in cl]}})
    # Два «нормальных» эпика: один почти доделан, второй закрыт с хвостом.
    for key, name, st, cat in (
            ("DEMO-181", "Эпик: перевод отчётности", "In Progress", WIP),
            ("DEMO-182", "Эпик: миграция витрин", "Done", DONE)):
        created = now - timedelta(days=rnd.uniform(120, 200))
        issues.append({"key": key, "fields": {
            "summary": name,
            "issuetype": {"name": "Epic", "subtask": False},
            "status": {"name": st, "statusCategory": {"key": cat}},
            "assignee": {"displayName": rnd.choice(people[:4])},
            "created": created.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            "resolutiondate": ((now - timedelta(days=10)).strftime(
                "%Y-%m-%dT%H:%M:%S.000+0000") if cat == DONE else None),
            "updated": (now - timedelta(days=rnd.uniform(15, 50))).strftime(
                "%Y-%m-%dT%H:%M:%S.000+0000"),
            "labels": [], "components": [],
        }, "changelog": {"histories": []}})

    payload = {"total": len(issues), "issues": issues}
    _atomic_write(args.out, json.dumps(payload, ensure_ascii=False, indent=2))
    return _emit({"output": os.path.abspath(args.out), "issues": len(issues),
                  "note": "формат совпадает с ответом Jira DC /rest/api/2/search"}, ok=True)


# --- CLI ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Аналитика Jira: выгрузки → метрики → автономный HTML-дашборд")
    sub = p.add_subparsers(dest="command", required=True)

    n = sub.add_parser("normalize", help="сырые выгрузки → канонический issues.json")
    n.add_argument("inputs", nargs="+", help="JSON-файлы ответов Jira/MCP")
    n.add_argument("--out", required=True)
    n.add_argument("--transitions", default=None,
                   help="JSON с историей статусов, если MCP не отдал changelog: "
                        "{\"KEY-1\": [{at,from,to}, ...]}")
    n.add_argument("--epic-links", default=None,
                   help="JSON со связями задача→эпик, если их нет в полях: "
                        "{\"ABC-2\": \"ABC-1\"}")
    n.add_argument("--epic-link-field", default=None,
                   help="имя customfield со ссылкой на эпик (Epic Link), "
                        "напр. customfield_10014; по умолчанию определяется сам")
    n.add_argument("--story-points-field", default=None,
                   help="имя customfield со story points, напр. customfield_10002")
    n.set_defaults(func=cmd_normalize)

    a = sub.add_parser("analyze", help="issues.json → metrics.json")
    a.add_argument("input")
    a.add_argument("--out", required=True)
    a.add_argument("--granularity", choices=["day", "week", "month"], default="week")
    a.add_argument("--since", default=None, help="начало периода, ISO-дата")
    a.add_argument("--until", default=None, help="конец периода, ISO-дата")
    a.add_argument("--now", default=None, help="переопределить «сейчас» (для тестов)")
    a.add_argument("--stale-days", type=int, default=14)
    a.add_argument("--longlived-days", type=int, default=90)
    a.add_argument("--aging-days", type=int, default=14,
                   help="запасной порог зависания, если cycle p85 не посчитан")
    a.add_argument("--wip-statuses", default=None,
                   help="названия рабочих статусов через запятую, напр. "
                        "\"В работе,На ревью\" — нужно, когда в выборке нет "
                        "ни одной открытой задачи и определить их не по чему")
    a.add_argument("--scope-items", choices=["leaf", "all"], default="leaf",
                   help="leaf (по умолчанию) — не считать эпики единицами "
                        "работы в метриках потока; all — считать всё подряд")
    a.add_argument("--batch-min", type=int, default=4,
                   help="сколько закрытий в один час считать пачкой")
    a.add_argument("--wip-warn", type=int, default=3,
                   help="сколько задач в работе на человека считать перегрузом")
    a.set_defaults(func=cmd_analyze)

    r = sub.add_parser("render", help="metrics.json → dashboard.html")
    r.add_argument("input")
    r.add_argument("--out", required=True)
    r.add_argument("--issues", default=None,
                   help="issues.json — нужен для гистограммы времени цикла")
    r.add_argument("--title", default="Аналитика Jira")
    r.add_argument("--insights", default=None,
                   help="insights.json с выводами модели (см. brief)")
    r.set_defaults(func=cmd_render)

    sm = sub.add_parser("summary", help="краткая сводка по metrics.json")
    sm.add_argument("input")
    sm.set_defaults(func=cmd_summary)

    b = sub.add_parser("brief", help="metrics.json → выжимка фактов для модели")
    b.add_argument("input")
    b.add_argument("--out", required=True)
    b.add_argument("--samples", type=int, default=12,
                   help="сколько задач-образцов класть в каждую категорию")
    b.set_defaults(func=cmd_brief)

    v = sub.add_parser("validate-insights",
                       help="проверить, что выводы модели опираются на реальные задачи")
    v.add_argument("input", help="insights.json")
    v.add_argument("--issues", required=True, help="issues.json")
    v.set_defaults(func=cmd_validate_insights)

    d = sub.add_parser("demo", help="сгенерировать синтетическую выгрузку Jira")
    d.add_argument("--out", required=True)
    d.add_argument("--count", type=int, default=180)
    d.add_argument("--days", type=int, default=120)
    d.add_argument("--seed", type=int, default=7)
    d.set_defaults(func=cmd_demo)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        # Вывод оборван (например, `| head`) — это не ошибка расчёта.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0



def cmd_summary(args: argparse.Namespace) -> int:
    """Короткая текстовая сводка по metrics.json — чтобы посмотреть результат
    и назвать числа в ответе, не открывая многомегабайтный JSON и не сочиняя
    для этого одноразовый скрипт."""
    if not os.path.isfile(args.input):
        return _emit({"error": f"файл не найден: {args.input}"}, ok=False)
    with open(args.input, encoding="utf-8") as fh:
        m = json.load(fh)
    flow, people, risks = m["flow"], m["people"], m["risks"]
    wait, dq = m.get("waiting", {}), m.get("data_quality", {})
    hier, epics = m.get("hierarchy", {}), m.get("epics", [])
    cyc, lead = flow["cycle_time"], flow["lead_time"]
    out = {
        "период": f'{m["period"]["from"][:10]} — {m["period"]["to"][:10]}',
        "задач_в_метриках": hier.get("counted_items", m["scope"]["total_issues"]),
        "создано": m["scope"]["created_in_period"],
        "закрыто": m["scope"]["resolved_in_period"],
        "открыто_сейчас": m["scope"]["open_now"],
        "пропускная_способность": flow["throughput_avg"],
        "lead_time": {"p50": lead["p50"], "p85": lead["p85"], "n": lead["count"]},
        "cycle_time": {"p50": cyc["p50"], "p85": cyc["p85"], "n": cyc["count"],
                       "способ": flow.get("cycle_time_method", {}).get("note")},
        "покрытие_changelog_pct": m["scope"]["changelog_coverage_pct"],
        "эффективность_потока_pct": wait.get("flow_efficiency_pct"),
        "bus_factor": people["bus_factor"],
        "доля_лидера_pct": people["top_share_pct"],
        "эпиков": hier.get("epics_total", 0),
        "эпиков_с_замечаниями": sum(1 for e in epics if e.get("flags")),
        "вне_эпиков": hier.get("orphans", 0),
        "риски": {k: len(risks.get(k, [])) for k in
                  ("eternal_wip", "aging_wip", "blocked", "overdue", "stale")},
        "качество_учёта": {"пачковых_закрытий": len(dq.get("batch_closures", [])),
                            "мгновенных_закрытий": dq.get("instant_count", 0)},
        "предупреждения": [f'[{w["severity"]}] {w["title"]}'
                           for w in m.get("warnings", [])],
    }
    return _emit(out, ok=True)


# --- Слой ИИ-анализа ---------------------------------------------------------
# Алгоритмы находят то, что описано порогами. Всё остальное — темы в названиях
# задач, причины выбросов, связки «этот эпик тормозит вон те задачи» — видит
# только модель. Чтобы её выводы были проверяемыми, а не красивым текстом,
# конвейер разделён на три шага: brief (факты) → insights (интерпретация) →
# validate-insights (заземление на реальные ключи задач).


def cmd_brief(args: argparse.Namespace) -> int:
    """Компактная выжимка фактов для модели: числа плюс образцы задач С
    НАЗВАНИЯМИ. Названия здесь ключевые — именно по ним видны темы и
    повторяющиеся сюжеты, которых нет ни в одной метрике."""
    if not os.path.isfile(args.input):
        return _emit({"error": f"файл не найден: {args.input}"}, ok=False)
    with open(args.input, encoding="utf-8") as fh:
        m = json.load(fh)
    n = args.samples

    flow, people, risks = m["flow"], m["people"], m["risks"]
    wait, dq = m.get("waiting", {}), m.get("data_quality", {})

    def sample(items: list[dict], fields: tuple[str, ...]) -> list[dict]:
        return [{k: it.get(k) for k in fields if it.get(k) is not None}
                for it in (items or [])[:n]]

    half = len(flow["buckets"]) // 2
    trend = None
    if half >= 2:
        trend = {"resolved_first_half": sum(flow["resolved"][:half]),
                 "resolved_second_half": sum(flow["resolved"][half:]),
                 "created_first_half": sum(flow["created"][:half]),
                 "created_second_half": sum(flow["created"][half:])}

    brief = {
        "period": m["period"],
        "scope": m["scope"],
        "flow": {
            "throughput_per_bucket": flow["throughput_avg"],
            "lead_time": flow["lead_time"],
            "cycle_time": flow["cycle_time"],
            "trend_halves": trend,
            "wip_over_time": flow["wip_over_time"],
            "buckets": flow["buckets"],
            "created": flow["created"],
            "resolved": flow["resolved"],
        },
        "waiting": {
            "flow_efficiency_pct": wait.get("flow_efficiency_pct"),
            "by_status_days": (wait.get("by_status_days") or [])[:8],
            "top_waiting_issues": sample(
                wait.get("top_issues"),
                ("key", "summary", "waiting_days", "waiting_share_pct")),
        },
        "people": {
            "rows": people["rows"][:15],
            "bus_factor": people["bus_factor"],
            "top_share_pct": people["top_share_pct"],
            "contributors": people["contributors"],
            "unassigned_open": people["unassigned_open"],
        },
        "risks": {
            k: sample(risks.get(k), ("key", "summary", "status", "assignee",
                                     "in_progress_days", "days_idle",
                                     "overdue_days", "age_days", "type"))
            for k in ("eternal_wip", "aging_wip", "blocked", "overdue",
                      "stale", "long_lived")
        },
        "data_quality": {
            "batch_closures": (dq.get("batch_closures") or [])[:5],
            "instant_closures": sample(dq.get("instant_closures"),
                                       ("key", "summary", "minutes")),
            "instant_count": dq.get("instant_count", 0),
        },
        "epics": [e for e in (m.get("epics") or []) if e["flags"] or e["children"]][:20],
        "hierarchy": m.get("hierarchy", {}),
        "distributions": {
            "status": m.get("status_distribution", {}),
            "type": m.get("type_distribution", {}),
        },
        "computed_warnings": [{"severity": w["severity"], "title": w["title"]}
                              for w in m.get("warnings", [])],
        "questions_to_answer": [
            "Какие СЮЖЕТЫ повторяются в названиях зависших и ожидающих задач? "
            "Есть ли общая тема (компонент, заказчик, тип работы)?",
            "Что происходит с инициативами: какие эпики стоят, какие закрыты "
            "с открытыми задачами, какая работа идёт мимо эпиков?",
            "Что на самом деле стоит за выбросами времени цикла — крупная "
            "работа, ожидание согласований или задача-контейнер?",
            "Отражают ли метрики реальную работу или практику ведения Jira? "
            "Смотри на пачковые и мгновенные закрытия.",
            "Что из найденного — следствие, а что причина? Какая ОДНА проблема, "
            "если её решить, вытянет остальные?",
            "Что именно предложить команде на ближайшей встрече — конкретное "
            "действие, а не «улучшить процесс».",
        ],
        "rules_for_you": [
            "Числа бери только из этой выжимки — не считай и не округляй заново.",
            "Каждую находку подкрепляй ключами задач из evidence_keys.",
            "Не повторяй computed_warnings дословно — углубляй их или "
            "объясняй причину.",
            "Если данных на вывод не хватает, ставь confidence=low и скажи, "
            "чего не хватает. Молчание лучше выдумки.",
        ],
    }
    _atomic_write(args.out, json.dumps(brief, ensure_ascii=False, indent=2))
    return _emit({
        "output": os.path.abspath(args.out),
        "bytes": os.path.getsize(args.out),
        "next": "прочитай выжимку, напиши insights.json по схеме из "
                "references/ai-insights.md, затем проверь его "
                "командой validate-insights",
    }, ok=True)


def cmd_validate_insights(args: argparse.Namespace) -> int:
    """Заземление: каждая находка модели должна ссылаться на существующие
    задачи. Так интерпретация остаётся проверяемой, а не убедительным текстом."""
    for path in (args.input, args.issues):
        if not os.path.isfile(path):
            return _emit({"error": f"файл не найден: {path}"}, ok=False)
    with open(args.input, encoding="utf-8") as fh:
        ins = json.load(fh)
    with open(args.issues, encoding="utf-8") as fh:
        known = {i["key"] for i in json.load(fh) if i.get("key")}

    problems: list[str] = []
    if not isinstance(ins, dict):
        return _emit({"error": "insights.json должен быть объектом"}, ok=False)
    if not str(ins.get("executive_summary", "")).strip():
        problems.append("пустой executive_summary")
    findings = ins.get("findings") or []
    if not findings:
        problems.append("нет ни одной находки (findings)")

    allowed_sev = {"critical", "serious", "warning", "info", "good"}
    allowed_conf = {"high", "medium", "low"}
    unknown_keys: list[str] = []
    for idx, f in enumerate(findings):
        tag = f.get("title") or f"#{idx + 1}"
        for field in ("title", "observation", "interpretation", "recommendation"):
            if not str(f.get(field, "")).strip():
                problems.append(f"находка «{tag}»: пустое поле {field}")
        if f.get("severity") not in allowed_sev:
            problems.append(f"находка «{tag}»: severity должен быть одним из "
                            f"{sorted(allowed_sev)}")
        if f.get("confidence") not in allowed_conf:
            problems.append(f"находка «{tag}»: confidence должен быть одним из "
                            f"{sorted(allowed_conf)}")
        keys = f.get("evidence_keys") or []
        bad = [k for k in keys if k not in known]
        if bad:
            unknown_keys.extend(bad)
            problems.append(f"находка «{tag}»: ключи задач не найдены в выборке: "
                            f"{', '.join(bad[:5])}")
        if not keys and f.get("confidence") == "high":
            problems.append(f"находка «{tag}»: высокая уверенность без ссылок "
                            "на задачи — либо добавь evidence_keys, либо снизь "
                            "confidence")

    ok = not problems
    return _emit({
        "findings": len(findings),
        "unknown_keys": sorted(set(unknown_keys)),
        "problems": problems,
        "verdict": ("выводы заземлены на реальные задачи" if ok
                    else "исправь замечания и проверь ещё раз"),
    }, ok=ok)


if __name__ == "__main__":
    raise SystemExit(main())
