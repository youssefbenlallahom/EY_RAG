#!/usr/bin/env python3
"""Clean extracted markdown for RAG chunking and write reports."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ey_rag.markdown_cleaners import CleanStats, clean_document


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean extracted markdown files for RAG ingestion."
    )
    parser.add_argument("--input-dir", default="output", help="Source markdown folder.")
    parser.add_argument("--output-dir", default="output_clean", help="Destination folder.")
    parser.add_argument(
        "--cache-file",
        default="output/image_extraction_cache.json",
        help="Vision extraction cache used to integrate missing image blocks.",
    )
    parser.add_argument(
        "--report-prefix",
        default="markdown_clean_report",
        help="Base name for report files written inside output-dir.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N markdown files.",
    )
    return parser.parse_args()


def load_cache(cache_path: Path) -> dict[str, str]:
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read cache file {cache_path}: {exc}") from exc


def pct_reduction(before: int, after: int) -> float:
    if before <= 0:
        return 0.0
    return round((before - after) / before * 100, 2)


def write_reports(
    output_dir: Path,
    report_prefix: str,
    rows: list[dict[str, object]],
    totals: CleanStats,
) -> tuple[Path, Path]:
    csv_path = output_dir / f"{report_prefix}.csv"
    md_path = output_dir / f"{report_prefix}.md"

    fieldnames = list(rows[0].keys()) if rows else []
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# Markdown Cleansing Report",
        "",
        f"Generated: {generated}",
        "",
        "## Corpus Summary",
        "",
        f"- Documents cleaned: {len(rows)}",
        f"- Characters before: {totals.chars_before:,}",
        f"- Characters after: {totals.chars_after:,}",
        f"- Character reduction: {pct_reduction(totals.chars_before, totals.chars_after)}%",
        f"- Image extractions integrated from cache: {totals.extractions_integrated}",
        f"- Duplicate paragraphs removed: {totals.duplicate_paragraphs_removed}",
        f"- Chart debris lines removed: {totals.chart_debris_lines_removed}",
        f"- HTML tables converted: {totals.html_tables_converted}",
        f"- Empty HTML tables removed: {totals.html_tables_removed_empty}",
        f"- BR chart tables split: {totals.br_tables_split}",
        f"- Incomplete extraction tables repaired: {totals.incomplete_tables_repaired}",
        f"- TOC lines cleaned: {totals.toc_lines_cleaned}",
        f"- Bullets normalized: {totals.bullets_normalized}",
        f"- False headings demoted: {totals.false_headings_demoted}",
        "",
        "## Page Types",
        "",
    ]
    for page_type, count in sorted(totals.page_types.items()):
        lines.append(f"- {page_type}: {count}")

    lines.extend(
        [
            "",
            "## Per-Document Summary",
            "",
            "| Document | Pages | Chars before | Chars after | Reduction % | Integrated | Dupes removed | Debris lines | Tables converted |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row['document']} | {row['pages']} | {row['chars_before']} | {row['chars_after']} | "
            f"{row['reduction_pct']} | {row['extractions_integrated']} | {row['duplicate_paragraphs_removed']} | "
            f"{row['chart_debris_lines_removed']} | {row['html_tables_converted']} |"
        )

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Cleansed files are written to `output_clean/`; originals in `output/` are untouched.",
            "- Pages are tagged with `<!-- PAGE_TYPE: ... -->` for downstream filtering.",
            "- Image extraction blocks are deduplicated against nearby chart debris and duplicate narrative text.",
            "",
            f"CSV details: `{csv_path.name}`",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, md_path


EXCLUDED_MARKDOWN = {
    "image_description_rag_quality_audit.md",
    "rag_extraction_quality_report.md",
}


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    cache_path = Path(args.cache_file).resolve()

    if not input_dir.exists():
        raise SystemExit(f"Input directory does not exist: {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    cache = load_cache(cache_path)

    md_files = sorted(input_dir.glob("*.md"))
    report_names = {f"{args.report_prefix}.md", f"{args.report_prefix}.csv"}
    md_files = [
        path
        for path in md_files
        if path.name not in report_names and path.name not in EXCLUDED_MARKDOWN
    ]
    if args.limit is not None:
        md_files = md_files[: args.limit]

    if not md_files:
        raise SystemExit(f"No markdown files found in {input_dir}")

    totals = CleanStats()
    rows: list[dict[str, object]] = []

    for md_path in md_files:
        document = md_path.stem
        original = md_path.read_text(encoding="utf-8")
        cleaned, stats = clean_document(original, document=document, cache=cache)
        (output_dir / md_path.name).write_text(cleaned, encoding="utf-8")
        totals.merge(stats)

        rows.append(
            {
                "document": document,
                "pages": stats.pages,
                "chars_before": stats.chars_before,
                "chars_after": stats.chars_after,
                "reduction_pct": pct_reduction(stats.chars_before, stats.chars_after),
                "extractions_integrated": stats.extractions_integrated,
                "duplicate_paragraphs_removed": stats.duplicate_paragraphs_removed,
                "chart_debris_lines_removed": stats.chart_debris_lines_removed,
                "html_tables_converted": stats.html_tables_converted,
                "html_tables_removed_empty": stats.html_tables_removed_empty,
                "br_tables_split": stats.br_tables_split,
                "incomplete_tables_repaired": stats.incomplete_tables_repaired,
                "toc_lines_cleaned": stats.toc_lines_cleaned,
                "bullets_normalized": stats.bullets_normalized,
                "false_headings_demoted": stats.false_headings_demoted,
                "page_types": "|".join(f"{key}:{value}" for key, value in sorted(stats.page_types.items())),
            }
        )
        print(
            f"Cleaned {md_path.name}: {stats.chars_before:,} -> {stats.chars_after:,} chars "
            f"({pct_reduction(stats.chars_before, stats.chars_after)}% reduction)"
        )

    csv_path, md_path = write_reports(output_dir, args.report_prefix, rows, totals)
    print("\nCleansing complete")
    print(f"Output folder: {output_dir}")
    print(f"Report: {md_path}")
    print(f"CSV: {csv_path}")


if __name__ == "__main__":
    main()
