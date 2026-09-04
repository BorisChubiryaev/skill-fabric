"""Юнит-тесты движка аналитики Jira.

Запуск:  python3 -m unittest scripts/test_jira_analytics.py -v
Зависимостей нет — только стандартная библиотека.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "jira_analytics.py")
sys.path.insert(0, HERE)
import jira_analytics as m  # noqa: E402


def _run(*args: str) -> tuple[int, dict]:
    p = subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True)
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, {"_stdout": p.stdout, "_stderr": p.stderr}


NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def jira_issue(key: str, created: datetime, *, resolved: datetime | None = None,
               status: str = "Open", cat: str = m.TODO, assignee: str | None = "Иван",
               updated: datetime | None = None, duedate: str | None = None,
               wip_at: datetime | None = None, flagged: bool = False,
               blocked_by: str | None = None) -> dict:
    hist = []
    if wip_at:
        hist.append({"created": wip_at.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                     "items": [{"field": "status", "fromString": "Open",
                                "toString": "In Progress"}]})
    if resolved:
        hist.append({"created": resolved.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                     "items": [{"field": "status", "fromString": "In Progress",
                                "toString": "Done"}]})
    fields = {
        "summary": f"Задача {key}",
        "issuetype": {"name": "Task"},
        "status": {"name": status, "statusCategory": {"key": cat}},
        "assignee": {"displayName": assignee} if assignee else None,
        "created": created.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
        "resolutiondate": resolved.strftime("%Y-%m-%dT%H:%M:%S.000+0000") if resolved else None,
        "updated": (updated or created).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
        "duedate": duedate,
        "labels": [], "components": [],
    }
    if flagged:
        fields["customfield_10100"] = {"value": "Impediment"}
    if blocked_by:
        fields["issuelinks"] = [{"type": {"inward": "is blocked by"},
                                 "inwardIssue": {"key": blocked_by}}]
    return {"key": key, "fields": fields, "changelog": {"histories": hist}}


class HelperTests(unittest.TestCase):
    def test_parse_dt_jira_dc_offset(self) -> None:
        d = m.parse_dt("2026-01-05T10:00:00.000+0300")
        self.assertIsNotNone(d)
        self.assertEqual((d.year, d.month, d.day), (2026, 1, 5))

    def test_parse_dt_variants(self) -> None:
        self.assertIsNotNone(m.parse_dt("2026-01-05T10:00:00Z"))
        self.assertIsNotNone(m.parse_dt("2026-01-05"))
        self.assertIsNone(m.parse_dt(None))
        self.assertIsNone(m.parse_dt(""))

    def test_percentile_nearest_rank(self) -> None:
        vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(m.percentile(vals, 50), 5)
        self.assertEqual(m.percentile(vals, 100), 10)
        self.assertIsNone(m.percentile([], 50))

    def test_nice_scale_gives_round_ticks(self) -> None:
        # Ось должна делиться на 4 круглых шага, иначе подписи вида 0,4,8,11,15.
        for raw in (15, 17, 23, 180, 7):
            ymax, step = m._nice_scale(raw)
            self.assertGreaterEqual(ymax, raw)
            self.assertAlmostEqual(ymax, step * 4, places=6)

    def test_xlabels_no_crowding_at_end(self) -> None:
        labels = [f"w{i}" for i in range(17)]
        idx = m._xlabels(labels, max_n=8)
        self.assertEqual(idx[-1], 16)
        self.assertGreaterEqual(idx[-1] - idx[-2], 1)
        self.assertLessEqual(len(idx), 9)

    def test_iter_raw_issues_tolerates_shapes(self) -> None:
        one = {"key": "A-1", "fields": {"summary": "x"}}
        self.assertEqual(len(m._iter_raw_issues({"issues": [one]})), 1)
        self.assertEqual(len(m._iter_raw_issues([{"issues": [one]}, {"issues": [one]}])), 2)
        self.assertEqual(len(m._iter_raw_issues({"data": {"issues": [one]}})), 1)
        self.assertEqual(len(m._iter_raw_issues(None)), 0)


class NormalizeTests(unittest.TestCase):
    def test_normalize_extracts_core_fields(self) -> None:
        raw = jira_issue("P-1", NOW - timedelta(days=10),
                         resolved=NOW - timedelta(days=2), status="Done",
                         cat=m.DONE, wip_at=NOW - timedelta(days=7),
                         flagged=True, blocked_by="P-9")
        n = m.normalize_issue(raw)
        self.assertEqual(n["key"], "P-1")
        self.assertEqual(n["status_category"], m.DONE)
        self.assertEqual(n["assignee"], "Иван")
        self.assertTrue(n["flagged"])
        self.assertEqual(n["blocked_by"], ["P-9"])
        self.assertEqual(len(n["changelog"]), 2)

    def test_normalize_sprint_from_greenhopper_string(self) -> None:
        raw = jira_issue("P-2", NOW)
        raw["fields"]["customfield_10007"] = [
            "com.atlassian.greenhopper.service.sprint.Sprint@1[id=5,name=Спринт 3,state=ACTIVE]"]
        self.assertEqual(m.normalize_issue(raw)["sprint"], "Спринт 3")

    def test_normalize_cli_dedupes_and_reports_changelog(self) -> None:
        tmp = tempfile.mkdtemp()
        raw = {"issues": [jira_issue("P-1", NOW - timedelta(days=5)),
                          jira_issue("P-1", NOW - timedelta(days=5)),
                          jira_issue("P-2", NOW - timedelta(days=4),
                                     wip_at=NOW - timedelta(days=3))]}
        src = os.path.join(tmp, "raw.json")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        rc, out = _run("normalize", src, "--out", os.path.join(tmp, "issues.json"))
        self.assertEqual(rc, 0)
        self.assertEqual(out["issues"], 2)          # дубль отброшен
        self.assertEqual(out["with_changelog"], 1)

    def test_normalize_rejects_empty(self) -> None:
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "e.json")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump({"issues": []}, fh)
        rc, out = _run("normalize", src, "--out", os.path.join(tmp, "o.json"))
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])


class AnalyzeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        issues = [
            # закрытые с историей → даёт cycle time
            jira_issue("P-1", NOW - timedelta(days=30), resolved=NOW - timedelta(days=20),
                       status="Done", cat=m.DONE, wip_at=NOW - timedelta(days=25)),
            jira_issue("P-2", NOW - timedelta(days=28), resolved=NOW - timedelta(days=10),
                       status="Done", cat=m.DONE, wip_at=NOW - timedelta(days=24),
                       assignee="Мария"),
            # просроченная
            jira_issue("P-3", NOW - timedelta(days=40), status="In Progress", cat=m.WIP,
                       duedate=(NOW - timedelta(days=5)).strftime("%Y-%m-%d"),
                       wip_at=NOW - timedelta(days=35)),
            # заблокированная
            jira_issue("P-4", NOW - timedelta(days=15), status="Blocked", cat=m.WIP,
                       flagged=True, wip_at=NOW - timedelta(days=14)),
            # без движения и без исполнителя
            jira_issue("P-5", NOW - timedelta(days=60), status="Open", cat=m.TODO,
                       assignee=None, updated=NOW - timedelta(days=45)),
        ]
        self.norm = [m.normalize_issue(i) for i in issues]
        self.ipath = os.path.join(self.tmp, "issues.json")
        with open(self.ipath, "w", encoding="utf-8") as fh:
            json.dump(self.norm, fh, ensure_ascii=False)
        self.mpath = os.path.join(self.tmp, "metrics.json")
        rc, self.res = _run("analyze", self.ipath, "--out", self.mpath,
                            "--now", NOW.isoformat())
        self.assertEqual(rc, 0, self.res)
        with open(self.mpath, encoding="utf-8") as fh:
            self.metrics = json.load(fh)

    def test_scope_and_flow(self) -> None:
        s = self.metrics["scope"]
        self.assertEqual(s["total_issues"], 5)
        self.assertEqual(s["resolved_in_period"], 2)
        self.assertEqual(s["open_now"], 3)
        self.assertEqual(s["changelog_coverage_pct"], 80.0)  # у P-5 истории нет

    def test_cycle_time_uses_changelog(self) -> None:
        cyc = self.metrics["flow"]["cycle_time"]
        self.assertEqual(cyc["count"], 2)
        # P-1: 25→20 = 5 дн., P-2: 24→10 = 14 дн.
        self.assertAlmostEqual(cyc["p50"], 5.0, delta=0.6)
        self.assertAlmostEqual(cyc["max"], 14.0, delta=0.6)

    def test_lead_time_differs_from_cycle(self) -> None:
        lead = self.metrics["flow"]["lead_time"]
        self.assertEqual(lead["count"], 2)
        self.assertGreater(lead["max"], self.metrics["flow"]["cycle_time"]["max"])

    def test_risks_detected(self) -> None:
        r = self.metrics["risks"]
        self.assertIn("P-3", [x["key"] for x in r["overdue"]])
        self.assertIn("P-4", [x["key"] for x in r["blocked"]])
        self.assertIn("P-5", [x["key"] for x in r["stale"]])
        # P-5 создана 60 дн. назад — при пороге по умолчанию (90 дн.)
        # долгожителем она НЕ считается.
        self.assertEqual(r["long_lived"], [])

    def test_longlived_threshold_is_configurable(self) -> None:
        out = os.path.join(self.tmp, "m2.json")
        rc, _ = _run("analyze", self.ipath, "--out", out,
                     "--now", NOW.isoformat(), "--longlived-days", "30")
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            keys = [x["key"] for x in json.load(fh)["risks"]["long_lived"]]
        self.assertIn("P-5", keys)

    def test_people_and_unassigned(self) -> None:
        p = self.metrics["people"]
        self.assertEqual(p["unassigned_open"], 1)
        self.assertGreaterEqual(p["contributors"], 1)

    def test_warnings_have_severity_and_sorted(self) -> None:
        ws = self.metrics["warnings"]
        self.assertTrue(ws)
        titles = [w["title"] for w in ws]
        self.assertIn("Просроченные сроки", titles)
        order = {"critical": 0, "serious": 1, "warning": 2, "info": 3, "good": 4}
        sevs = [order[w["severity"]] for w in ws]
        self.assertEqual(sevs, sorted(sevs))

    def test_warning_counts_match_tiles(self) -> None:
        # Числа в тексте предупреждения должны совпадать с KPI-плитками.
        w = next(w for w in self.metrics["warnings"]
                 if "Бэклог" in w["title"] or "разгребает" in w["title"])
        ev = w["evidence"]
        self.assertEqual(ev["created"], self.metrics["scope"]["created_in_period"])
        self.assertEqual(ev["resolved"], self.metrics["scope"]["resolved_in_period"])

    def test_render_is_self_contained(self) -> None:
        out = os.path.join(self.tmp, "d.html")
        rc, res = _run("render", self.mpath, "--out", out, "--issues", self.ipath)
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<script", html)
        self.assertIn("<svg", html)
        self.assertIn("Просроченные", html)


class DetectorTests(unittest.TestCase):
    """Детекторы, появившиеся из разбора реального прогона."""

    def test_status_durations_and_waiting(self) -> None:
        raw = jira_issue("W-1", NOW - timedelta(days=20),
                         resolved=NOW - timedelta(days=1), status="Done",
                         cat=m.DONE, wip_at=NOW - timedelta(days=18))
        # Вставляем «уход в ожидание» на 10 дней.
        raw["changelog"]["histories"].insert(1, {
            "created": (NOW - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            "items": [{"field": "status", "fromString": "In Progress",
                       "toString": "Need Info"}]})
        raw["changelog"]["histories"].insert(2, {
            "created": (NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            "items": [{"field": "status", "fromString": "Need Info",
                       "toString": "In Progress"}]})
        n = m.normalize_issue(raw)
        durs = m.status_durations(n, NOW)
        self.assertIn("Need Info", durs)
        self.assertAlmostEqual(durs["Need Info"], 10.0, delta=0.6)

    def test_waiting_pattern_matches_ru_and_en(self) -> None:
        for st in ("Need Info", "На паузе", "Ожидание ответа", "Blocked", "Pending"):
            self.assertTrue(m.WAITING_PATTERN.search(st), st)
        for st in ("In Progress", "Done", "Готово"):
            self.assertFalse(m.WAITING_PATTERN.search(st), st)

    def test_batch_closures_detected(self) -> None:
        at = NOW - timedelta(days=3)
        issues = [m.normalize_issue(jira_issue(f"B-{i}", NOW - timedelta(days=10),
                                               resolved=at + timedelta(minutes=i),
                                               status="Done", cat=m.DONE))
                  for i in range(6)]
        batches = m.detect_batch_closures(issues, min_batch=4)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["count"], 6)

    def test_instant_closures_detected(self) -> None:
        c = NOW - timedelta(days=2)
        issues = [m.normalize_issue(jira_issue("I-1", c, resolved=c + timedelta(minutes=5),
                                               status="Done", cat=m.DONE)),
                  m.normalize_issue(jira_issue("I-2", c, resolved=c + timedelta(days=4),
                                               status="Done", cat=m.DONE))]
        inst = m.detect_instant_closures(issues)
        self.assertEqual([x["key"] for x in inst], ["I-1"])

    def test_transitions_file_merged_in_normalize(self) -> None:
        tmp = tempfile.mkdtemp()
        raw = jira_issue("T-1", NOW - timedelta(days=10),
                         resolved=NOW - timedelta(days=2), status="Done", cat=m.DONE)
        raw["changelog"] = {"histories": []}          # MCP не отдал историю
        src = os.path.join(tmp, "raw.json")
        with open(src, "w", encoding="utf-8") as fh:
            json.dump({"issues": [raw]}, fh)
        trans = os.path.join(tmp, "trans.json")
        with open(trans, "w", encoding="utf-8") as fh:
            json.dump({"T-1": [{"at": (NOW - timedelta(days=8)).isoformat(),
                                "from": "Open", "to": "In Progress"}]}, fh)
        rc, out = _run("normalize", src, "--transitions", trans,
                       "--out", os.path.join(tmp, "i.json"))
        self.assertEqual(rc, 0)
        self.assertEqual(out["changelog_merged_from_file"], 1)
        self.assertEqual(out["changelog_coverage_pct"], 100.0)


class HierarchyTests(unittest.TestCase):
    """Эпики, задачи под ними и подзадачи."""

    def _raw(self) -> dict:
        def issue(key, typ, subtask=False, parent=None, epic=None,
                  cat=m.TODO, status="Open", resolved=None):
            f = {"summary": f"Тема {key}",
                 "issuetype": {"name": typ, "subtask": subtask},
                 "status": {"name": status, "statusCategory": {"key": cat}},
                 "created": (NOW - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                 "updated": (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")}
            if resolved:
                f["resolutiondate"] = resolved.strftime("%Y-%m-%dT%H:%M:%S.000+0000")
            if parent:
                f["parent"] = {"key": parent}
            if epic:
                f["customfield_10014"] = epic
            return {"key": key, "fields": f, "changelog": {"histories": []}}

        return {"issues": [
            issue("EP-1", "Epic", cat=m.WIP, status="In Progress"),
            issue("EP-2", "Epic", cat=m.DONE, status="Done",
                  resolved=NOW - timedelta(days=5)),
            issue("TS-1", "Story", epic="EP-1", cat=m.DONE, status="Done",
                  resolved=NOW - timedelta(days=6)),
            issue("TS-2", "Task", parent="EP-1"),
            issue("SB-1", "Sub-task", subtask=True, parent="TS-1"),
            issue("TS-3", "Task", epic="EP-2"),
            issue("TS-4", "Task"),
        ]}

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.raw = os.path.join(self.tmp, "raw.json")
        with open(self.raw, "w", encoding="utf-8") as fh:
            json.dump(self._raw(), fh, ensure_ascii=False)
        self.iss = os.path.join(self.tmp, "i.json")
        rc, self.norm = _run("normalize", self.raw, "--out", self.iss)
        self.assertEqual(rc, 0, self.norm)

    def test_epic_links_resolved_three_ways(self) -> None:
        with open(self.iss, encoding="utf-8") as fh:
            by = {i["key"]: i for i in json.load(fh)}
        self.assertEqual(by["TS-1"]["epic_key"], "EP-1")   # Epic Link
        self.assertEqual(by["TS-2"]["epic_key"], "EP-1")   # через parent
        self.assertEqual(by["SB-1"]["epic_key"], "EP-1")   # подзадача наследует
        self.assertIsNone(by["TS-4"]["epic_key"])         # вне эпиков
        self.assertTrue(by["SB-1"]["is_subtask"])

    def test_normalize_reports_hierarchy(self) -> None:
        hi = self.norm["hierarchy"]
        self.assertEqual(hi["epics"], 2)
        self.assertEqual(hi["subtasks"], 1)
        self.assertEqual(hi["orphans"], 1)

    def test_epics_excluded_from_flow_by_default(self) -> None:
        met = os.path.join(self.tmp, "m.json")
        rc, _ = _run("analyze", self.iss, "--out", met, "--now", NOW.isoformat())
        self.assertEqual(rc, 0)
        with open(met, encoding="utf-8") as fh:
            metrics = json.load(fh)
        self.assertEqual(metrics["hierarchy"]["counted_items"], 5)   # 7 − 2 эпика
        self.assertEqual(metrics["hierarchy"]["epics_excluded_from_flow"], 2)

    def test_scope_all_includes_epics(self) -> None:
        met = os.path.join(self.tmp, "m2.json")
        rc, _ = _run("analyze", self.iss, "--out", met, "--now", NOW.isoformat(),
                     "--scope-items", "all")
        self.assertEqual(rc, 0)
        with open(met, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["hierarchy"]["counted_items"], 7)

    def test_epic_rollup_and_inconsistency_flag(self) -> None:
        met = os.path.join(self.tmp, "m3.json")
        _run("analyze", self.iss, "--out", met, "--now", NOW.isoformat())
        with open(met, encoding="utf-8") as fh:
            metrics = json.load(fh)
        epics = {e["key"]: e for e in metrics["epics"]}
        self.assertEqual(epics["EP-1"]["children"], 3)
        self.assertEqual(epics["EP-1"]["done"], 1)
        # E-2 закрыт, а T-3 под ним открыт — это должно быть поймано.
        self.assertIn("эпик закрыт, но задачи открыты", epics["EP-2"]["flags"])
        self.assertTrue(any("Эпик закрыт" in w["title"]
                            for w in metrics["warnings"]))


class AiLayerTests(unittest.TestCase):
    """Слой анализа: выжимка фактов и заземление выводов."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        raw = os.path.join(self.tmp, "raw.json")
        _run("demo", "--out", raw, "--count", "60", "--days", "90")
        self.iss = os.path.join(self.tmp, "i.json")
        _run("normalize", raw, "--out", self.iss)
        self.met = os.path.join(self.tmp, "m.json")
        _run("analyze", self.iss, "--out", self.met)

    def _insights(self, **over) -> str:
        base = {
            "executive_summary": "Сводка для руководителя.",
            "findings": [{
                "title": "Находка", "severity": "warning",
                "observation": "Что видно", "interpretation": "Что значит",
                "recommendation": "Что делать",
                "evidence_keys": ["DEMO-1"], "confidence": "high"}],
            "next_steps": ["Шаг"],
        }
        base.update(over)
        path = os.path.join(self.tmp, f"ins{len(over)}{id(over)}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(base, fh, ensure_ascii=False)
        return path

    def test_brief_contains_titles_for_theme_finding(self) -> None:
        out = os.path.join(self.tmp, "b.json")
        rc, res = _run("brief", self.met, "--out", out)
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            b = json.load(fh)
        for section in ("flow", "waiting", "people", "risks", "data_quality",
                        "questions_to_answer", "rules_for_you"):
            self.assertIn(section, b)
        # Названия задач обязаны быть в выжимке — по ним модель ищет сюжеты.
        sample = b["risks"]["eternal_wip"] or b["risks"]["aging_wip"]
        if sample:
            self.assertIn("summary", sample[0])

    def test_valid_insights_pass(self) -> None:
        rc, out = _run("validate-insights", self._insights(), "--issues", self.iss)
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])

    def test_fabricated_issue_key_is_rejected(self) -> None:
        path = self._insights(findings=[{
            "title": "Выдумка", "severity": "serious", "observation": "o",
            "interpretation": "i", "recommendation": "r",
            "evidence_keys": ["DEMO-999999"], "confidence": "high"}])
        rc, out = _run("validate-insights", path, "--issues", self.iss)
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertIn("DEMO-999999", out["unknown_keys"])

    def test_high_confidence_without_evidence_rejected(self) -> None:
        path = self._insights(findings=[{
            "title": "Без доказательств", "severity": "warning", "observation": "o",
            "interpretation": "i", "recommendation": "r",
            "evidence_keys": [], "confidence": "high"}])
        rc, out = _run("validate-insights", path, "--issues", self.iss)
        self.assertEqual(rc, 1)
        self.assertTrue(any("уверенность" in p for p in out["problems"]))

    def test_bad_severity_rejected(self) -> None:
        path = self._insights(findings=[{
            "title": "Плохой уровень", "severity": "катастрофа", "observation": "o",
            "interpretation": "i", "recommendation": "r",
            "evidence_keys": [], "confidence": "low"}])
        rc, out = _run("validate-insights", path, "--issues", self.iss)
        self.assertEqual(rc, 1)

    def test_render_includes_ai_section(self) -> None:
        html = os.path.join(self.tmp, "d.html")
        rc, res = _run("render", self.met, "--out", html, "--issues", self.iss,
                       "--insights", self._insights())
        self.assertEqual(rc, 0)
        self.assertEqual(res["ai_findings"], 1)
        with open(html, encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("Анализ: что происходит и что делать", page)
        self.assertIn("Сводка для руководителя", page)
        self.assertNotIn("https://", page)


class PipelineTests(unittest.TestCase):
    def test_demo_pipeline_end_to_end(self) -> None:
        tmp = tempfile.mkdtemp()
        raw = os.path.join(tmp, "raw.json")
        rc, gen = _run("demo", "--out", raw, "--count", "60", "--days", "60")
        self.assertEqual(rc, 0)
        iss = os.path.join(tmp, "i.json")
        rc, o1 = _run("normalize", raw, "--out", iss)
        self.assertEqual(rc, 0)
        # demo добавляет эпики сверх --count, поэтому сверяемся с тем, что
        # он реально сгенерировал, а не с запрошенным числом.
        self.assertEqual(o1["issues"], gen["issues"])
        self.assertGreaterEqual(o1["issues"], 60)
        met = os.path.join(tmp, "m.json")
        rc, o2 = _run("analyze", iss, "--out", met)
        self.assertEqual(rc, 0)
        self.assertTrue(o2["ok"])
        html = os.path.join(tmp, "d.html")
        rc, o3 = _run("render", met, "--out", html, "--issues", iss)
        self.assertEqual(rc, 0)
        self.assertGreater(o3["bytes"], 5000)


if __name__ == "__main__":
    unittest.main()
