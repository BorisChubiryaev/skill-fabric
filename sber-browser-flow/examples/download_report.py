"""Пример сценария: зайти на портал, найти отчёт и скачать его.

Запуск:
  python3 scripts/run_flow.py run examples/download_report.py --out runs

Замените URL и селекторы на реальные — это шаблон, показывающий стиль.
"""


def run(s):
    with s.step("открыть портал"):
        s.goto("https://internal.example/reports")
        s.wait_for("#search")

    with s.step("найти отчёт"):
        s.fill("#search", "квартальный отчёт")
        s.press("#search", "Enter")
        s.wait_for("table.results tbody tr")

    with s.step("проверить, что нашли"):
        rows = s.extract("table.results tbody tr",
                         {"название": "td.name", "дата": "td.date"})
        assert rows, "результатов поиска нет — проверьте запрос"

    with s.step("скачать"):
        path = s.download("a.export-xlsx", save_as="quarterly.xlsx")
        # path — абсолютный путь сохранённого файла
