"""Model comparison table for the economics slide (role 6), built from eval reports.

    uv run python -m evals.summary                    # print the table
    uv run python -m evals.summary --write docs/ML_EVAL.md

Takes the latest full-set report (no -k / --category filter) per model and split.
"Факт" cost is what the run was billed with (0 on the prepaid GigaChat package);
"по тарифу" recomputes each request from its real tokens at a checked price, shown separately
as AGENTS §15 asks: the free quota and the cost of computation are different numbers.
Input tokens are split into billed ones and the prompt prefix served from the provider's cache.
"""

import argparse
import json
from pathlib import Path
from typing import Any

from evals.stats import describe

REPORTS_DIR = Path(__file__).parent / "reports"

# RUB per 1M tokens incl. VAT (input, output), by the provider a report was run with.
# GigaChat API, legal entities, synchronous pay-as-you-go: 0.65 RUB per 1000 tokens, one price for input
# and output; the 30M package (19 500 RUB) gives the same price. Cached tokens are not billed.
# Checked 26.10.2026: developers.sber.ru/docs/ru/gigachat/tariffs/legal-tariffs, .../guides/counting-tokens.
# Cloud.ru Foundation Models price list v260915; its cache billing is not checked.
LIST_PRICES_RUB_PER_1M: dict[str, dict[str, tuple[float, float]]] = {
    "gigachat": {"GigaChat-2-Max": (650.0, 650.0)},
    "cloudru": {
        "GigaChat-2-Max": (569.34, 569.34),
        "ai-sage/GigaChat3.5-432B-A28B-Reasoning": (96.22, 288.60),
        "GigaChat-3-Pro": (73.03, 176.39),
    },
}
PRICE_SOURCE = (
    "GigaChat API для юрлиц, 0,65 ₽ за 1000 токенов, кэш не тарифицируется; Cloud.ru v260915; с НДС"
)


def _is_full_set(report: Any) -> bool:
    """LLM eval report over the whole set; other JSON in the folder (e.g. ASR runs) is skipped."""
    if not isinstance(report, dict):
        return False
    flt = report.get("filter", {})
    return "summary" in report and not flt.get("k") and not flt.get("category")


def latest_reports(reports_dir: Path = REPORTS_DIR) -> list[tuple[str, dict[str, Any]]]:
    """Newest report per model, split filter and MCP backend; file names start with a sortable timestamp."""
    chosen: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}
    for path in sorted(reports_dir.glob("*.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        if _is_full_set(report):
            # Runs against the stub and the real MCP server are different measurements.
            key = (report["model"], report["filter"]["split"], report.get("mcp", "stub"))
            chosen[key] = (path.name, report)
    return sorted(chosen.values(), key=lambda item: (item[1]["model"], item[1]["filter"]["split"]))


def _list_price_costs(report: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, float] | None:
    price = LIST_PRICES_RUB_PER_1M.get(report.get("provider", "gigachat"), {}).get(report["model"])
    if price is None:
        return None
    return describe([(r["input_tokens"] * price[0] + r["output_tokens"] * price[1]) / 1e6 for r in rows])


def _row(name: str, report: dict[str, Any], split: str) -> str | None:
    summary = report["summary"].get(split)
    if not summary or not summary["n"]:
        return None
    rows = [r for r in report["results"] if not r["infra_error"] and (split == "all" or r["split"] == split)]
    lat, cost = summary["latency_s"], summary["cost_rub"]
    tin, tout = summary["input_tokens"], summary["output_tokens"]
    # Older reports have no behaviour score and no cache count.
    behavior = summary.get("passed_behavior", summary["passed"])
    cached = summary.get("cached_input_tokens", {}).get("mean", 0.0)
    listed = _list_price_costs(report, rows)
    listed_text = f"{listed['mean']:.3f} / {listed['median']:.3f} / {listed['p95']:.3f}" if listed else "—"
    return (
        f"| {report['model']} | {split} | {summary['n']} (×{report['repeat']}) "
        f"| {summary['passed']}/{summary['n']} ({summary['accuracy']:.0%}) "
        f"| {behavior}/{summary['n']} ({behavior / summary['n']:.0%}) "
        f"| {lat['mean']:.1f} / {lat['median']:.1f} / {lat['p95']:.1f} "
        f"| {tin['mean']:.0f} / {cached:.0f} / {tout['mean']:.0f} "
        f"| {cost['mean']:.3f} / {cost['median']:.3f} / {cost['p95']:.3f} "
        f"| {listed_text} | {summary['infra_errors']} | {report.get('mcp', 'stub')} | {name} |"
    )


def build_table(reports_dir: Path = REPORTS_DIR) -> str:
    header = (
        "| Модель | Выборка | N запусков | Точность строго | По поведению "
        "| Латентность, с: среднее / медиана / p95 "
        "| Токены на запрос, вход без кэша / кэш / выход (среднее) "
        "| Стоимость факт, ₽: среднее / медиана / p95 "
        "| По тарифу, ₽: среднее / медиана / p95 | Сбои API | MCP | Отчёт |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    lines = [header]
    package_tokens = 0
    for name, report in latest_reports(reports_dir):
        splits = ["tune", "holdout"] if report["filter"]["split"] == "all" else [report["filter"]["split"]]
        lines += [row for split in splits if (row := _row(name, report, split))]
        if report.get("provider", "gigachat") == "gigachat":
            package_tokens += sum(
                r["input_tokens"] + r["output_tokens"] for r in report["results"] if not r["infra_error"]
            )
    lines += [
        "",
        f"Тариф: {PRICE_SOURCE}.",
        # AGENTS §15: the free quota is shown apart from the cost of computation.
        f"Предоплаченный пакет GigaChat: стоимость «факт» 0 ₽; прогоны из таблицы израсходовали "
        f"{package_tokens:,} тарифицируемых токенов (вход без кэша + выход).".replace(",", " "),
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--write", type=Path, help="replace the table between markers in this Markdown file")
    args = parser.parse_args()
    table = build_table()
    if args.write is None:
        print(table)
        return
    begin, end = "<!-- eval-table:begin -->", "<!-- eval-table:end -->"
    text = args.write.read_text(encoding="utf-8")
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    args.write.write_text(f"{head}{begin}\n{table}\n{end}{tail}", encoding="utf-8")
    print(f"Таблица обновлена: {args.write}")


if __name__ == "__main__":
    main()
