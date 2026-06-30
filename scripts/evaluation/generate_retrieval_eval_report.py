"""Generate a PDF report comparing RAG retrieval results against gold scenarios.

This script is intentionally offline and deterministic. It does not call an LLM.
Run it only after ingestion and retrieval experiments are complete.
"""

from __future__ import annotations

import argparse
import html
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


DEFAULT_INDEXING_TYPES = [
    "langchain_markdown_recursive",
    "llamaindex_semantic",
    "haystack_document_splitter",
    "chonkie_semantic",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a professional PDF report for retrieval benchmark results."
    )
    parser.add_argument(
        "--scenarios",
        default="eval/retrieval_benchmark_scenarios.json",
        help="Path to the gold scenario JSON file.",
    )
    parser.add_argument(
        "--results",
        required=True,
        help="Path to a retrieval results JSON file following eval/retrieval_results_template.json.",
    )
    parser.add_argument(
        "--output",
        default="output/pdf/retrieval_evaluation_report.pdf",
        help="Output PDF path.",
    )
    parser.add_argument(
        "--workspace-root",
        default=".",
        help="Workspace root used to resolve relative image paths.",
    )
    parser.add_argument(
        "--max-answer-chars",
        type=int,
        default=600,
        help="Maximum retrieved-answer characters shown per row.",
    )
    parser.add_argument(
        "--include-ground-truth-images",
        action="store_true",
        help="Render ground-truth images in scenario sections when available.",
    )
    parser.add_argument(
        "--max-retrieved-images-per-row",
        type=int,
        default=1,
        help="Maximum retrieved image thumbnails shown inside each comparison-table row.",
    )
    return parser.parse_args()


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def norm(text: Any) -> str:
    return str(text or "").lower().strip()


def join_result_text(result: dict[str, Any]) -> str:
    pieces: list[str] = [str(result.get("retrieved_answer") or "")]
    for context in result.get("retrieved_contexts") or []:
        pieces.append(str(context.get("source_path") or ""))
        pieces.append(str(context.get("text") or ""))
    for image_path in result.get("retrieved_image_paths") or []:
        pieces.append(str(image_path))
    return "\n".join(pieces)


def truncate(text: Any, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 20].rstrip() + " ... [truncated]"


def paragraph(text: Any, style: ParagraphStyle) -> Paragraph:
    safe = html.escape(str(text or "")).replace("\n", "<br/>")
    return Paragraph(safe, style)


def resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def summarize_contexts(result: dict[str, Any], max_contexts: int = 3) -> str:
    contexts = result.get("retrieved_contexts") or []
    if not contexts:
        return "No retrieved contexts provided."

    lines: list[str] = []
    for context in contexts[:max_contexts]:
        rank = context.get("rank", "?")
        score = context.get("score", "")
        source = context.get("source_path", "")
        page = context.get("page", "")
        chunk = context.get("chunk_id", "")
        label = f"#{rank}"
        if score not in ("", None):
            label += f" score={score}"
        if page not in ("", None):
            label += f" page={page}"
        if chunk:
            label += f" chunk={chunk}"
        lines.append(f"{label}: {source}")
    return "\n".join(lines)


def expected_text_sources(scenario: dict[str, Any]) -> list[str]:
    return [
        str(item.get("source_path"))
        for item in scenario.get("evidence", [])
        if item.get("type") == "text" and item.get("source_path")
    ]


def expected_image_sources(scenario: dict[str, Any]) -> list[str]:
    return [
        str(item.get("source_path"))
        for item in scenario.get("evidence", [])
        if item.get("type") == "image" and item.get("source_path")
    ]


def path_matches(expected: str, actual: str) -> bool:
    expected_norm = expected.replace("\\", "/").lower()
    actual_norm = actual.replace("\\", "/").lower()
    return expected_norm in actual_norm or actual_norm.endswith(expected_norm)


def score_result(
    scenario: dict[str, Any],
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    if result is None:
        return {
            "auto_score": 0,
            "display_score": 0,
            "coverage_score": 0,
            "source_score": 0,
            "image_score": 0,
            "precision_score": 0,
            "found_terms": [],
            "missing_terms": scenario.get("must_contain", []),
            "evaluation_notes": "No result was provided for this scenario and indexing type.",
        }
    if result.get("retrieval_status") == "collection_missing":
        return {
            "auto_score": 0,
            "display_score": result.get("human_score") if result.get("human_score") is not None else 0,
            "coverage_score": 0,
            "source_score": 0,
            "image_score": 0,
            "precision_score": 0,
            "found_terms": [],
            "missing_terms": scenario.get("must_contain", []),
            "evaluation_notes": "Collection was not available for this indexing type: "
            + "; ".join(str(item) for item in result.get("known_errors", [])),
        }

    combined = norm(join_result_text(result))
    must_terms = [str(term) for term in scenario.get("must_contain", [])]
    found_terms = [term for term in must_terms if norm(term) in combined]
    missing_terms = [term for term in must_terms if norm(term) not in combined]
    coverage_ratio = len(found_terms) / len(must_terms) if must_terms else 1.0
    coverage_score = round(40 * coverage_ratio, 1)

    contexts = result.get("retrieved_contexts") or []
    actual_text_sources = [str(ctx.get("source_path") or "") for ctx in contexts]
    expected_sources = expected_text_sources(scenario)
    matched_sources = 0
    for expected in expected_sources:
        if any(path_matches(expected, actual) for actual in actual_text_sources):
            matched_sources += 1
    source_ratio = matched_sources / len(expected_sources) if expected_sources else 1.0
    source_score = round(25 * source_ratio, 1)

    expected_images = expected_image_sources(scenario)
    actual_images = [str(path) for path in result.get("retrieved_image_paths") or []]
    if scenario.get("should_retrieve_image"):
        image_matches = 0
        for expected in expected_images:
            if any(path_matches(expected, actual) for actual in actual_images):
                image_matches += 1
        if expected_images:
            image_score = round(20 * image_matches / len(expected_images), 1)
        else:
            image_score = 10.0 if actual_images else 0.0
    else:
        image_score = 20.0

    precision_score = 15.0
    if not str(result.get("retrieved_answer") or "").strip():
        precision_score = 0.0
    if result.get("known_errors"):
        precision_score = max(0.0, precision_score - 5.0 * len(result["known_errors"]))
    if result.get("unsupported_claims"):
        precision_score = max(0.0, precision_score - 5.0 * len(result["unsupported_claims"]))

    auto_score = round(coverage_score + source_score + image_score + precision_score, 1)
    human_score = result.get("human_score")
    display_score = human_score if human_score is not None else auto_score

    notes = [
        f"Found {len(found_terms)}/{len(must_terms)} required terms.",
        f"Matched {matched_sources}/{len(expected_sources)} expected text sources.",
    ]
    if scenario.get("should_retrieve_image"):
        notes.append(f"Retrieved {len(actual_images)} image path(s); expected {len(expected_images)}.")
    if result.get("human_notes"):
        notes.append(f"Human notes: {result['human_notes']}")
    if missing_terms:
        notes.append("Missing terms: " + ", ".join(missing_terms[:8]))

    return {
        "auto_score": auto_score,
        "display_score": display_score,
        "coverage_score": coverage_score,
        "source_score": source_score,
        "image_score": image_score,
        "precision_score": precision_score,
        "found_terms": found_terms,
        "missing_terms": missing_terms,
        "evaluation_notes": "\n".join(notes),
    }


def image_flowable(root: Path, image_path: str, max_width: float, max_height: float) -> Any:
    resolved = resolve_path(root, image_path)
    if not resolved.exists():
        return paragraph(f"Image not found: {image_path}", getSampleStyleSheet()["BodyText"])

    image = Image(str(resolved))
    width_ratio = max_width / image.imageWidth
    height_ratio = max_height / image.imageHeight
    scale = min(width_ratio, height_ratio, 1.0)
    image.drawWidth = image.imageWidth * scale
    image.drawHeight = image.imageHeight * scale
    return image


def retrieved_evidence_cell(
    result: dict[str, Any] | None,
    evidence_text: str,
    styles: dict[str, ParagraphStyle],
    workspace_root: Path,
    max_images: int = 1,
) -> Any:
    if result is None:
        return paragraph(evidence_text, styles["small"])

    image_paths = [str(path) for path in result.get("retrieved_image_paths") or []]
    if not image_paths:
        return paragraph(evidence_text, styles["small"])

    cell_items: list[Any] = [paragraph(evidence_text, styles["small"])]
    for image_path in image_paths[:max(0, max_images)]:
        cell_items.append(Spacer(1, 0.08 * cm))
        cell_items.append(image_flowable(workspace_root, image_path, 3.8 * cm, 2.4 * cm))
        cell_items.append(paragraph(image_path, styles["small"]))
    if len(image_paths) > max_images:
        cell_items.append(paragraph(f"+ {len(image_paths) - max_images} more image(s)", styles["small"]))
    return cell_items


def add_page_number(canvas: Any, doc: Any) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#606060"))
    canvas.drawRightString(
        landscape(A4)[0] - 1.0 * cm,
        0.6 * cm,
        f"Page {doc.page}",
    )
    canvas.restoreState()


def build_summary_table(
    results_by_index: dict[str, list[dict[str, Any]]],
    styles: dict[str, ParagraphStyle],
) -> Table:
    rows: list[list[Any]] = [
        [
            paragraph("Indexing type", styles["table_header"]),
            paragraph("Results", styles["table_header"]),
            paragraph("Average auto score", styles["table_header"]),
            paragraph("Hybrid image hit rate", styles["table_header"]),
        ]
    ]

    for indexing_type in sorted(results_by_index):
        scored = results_by_index[indexing_type]
        if scored:
            avg = sum(item["score"]["auto_score"] for item in scored) / len(scored)
            hybrid = [item for item in scored if item["scenario"].get("should_retrieve_image")]
            if hybrid:
                hit_rate = sum(1 for item in hybrid if item["score"]["image_score"] >= 20) / len(hybrid)
                hit_text = f"{hit_rate:.0%}"
            else:
                hit_text = "N/A"
            avg_text = f"{avg:.1f}"
        else:
            avg_text = "N/A"
            hit_text = "N/A"

        rows.append(
            [
                paragraph(indexing_type, styles["body"]),
                paragraph(str(len(scored)), styles["body"]),
                paragraph(avg_text, styles["body"]),
                paragraph(hit_text, styles["body"]),
            ]
        )

    table = Table(rows, colWidths=[8.5 * cm, 3 * cm, 4 * cm, 4 * cm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#242424")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D0D0D0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F7F7")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def build_report(
    scenarios_doc: dict[str, Any],
    results_doc: dict[str, Any],
    output_path: Path,
    workspace_root: Path,
    max_answer_chars: int,
    include_ground_truth_images: bool,
    max_retrieved_images_per_row: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    base_styles = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle(
            "Title",
            parent=base_styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            alignment=TA_CENTER,
            spaceAfter=14,
        ),
        "h1": ParagraphStyle(
            "H1",
            parent=base_styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=17,
            spaceBefore=12,
            spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "H2",
            parent=base_styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            spaceBefore=8,
            spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base_styles["BodyText"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            alignment=TA_LEFT,
        ),
        "small": ParagraphStyle(
            "Small",
            parent=base_styles["BodyText"],
            fontName="Helvetica",
            fontSize=7,
            leading=8.5,
            alignment=TA_LEFT,
        ),
        "table_header": ParagraphStyle(
            "TableHeader",
            parent=base_styles["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=colors.white,
        ),
    }

    scenario_list = scenarios_doc.get("scenarios", [])
    scenarios = {scenario["id"]: scenario for scenario in scenario_list}
    index_types = results_doc.get("indexing_types") or scenarios_doc.get("indexing_types_to_compare") or DEFAULT_INDEXING_TYPES

    result_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for result in results_doc.get("results", []):
        key = (str(result.get("scenario_id")), str(result.get("indexing_type")))
        result_lookup[key] = result

    scored_results_by_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    scored_by_scenario: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for scenario in scenario_list:
        for indexing_type in index_types:
            result = result_lookup.get((scenario["id"], indexing_type))
            score = score_result(scenario, result)
            if result is not None:
                scored = {"scenario": scenario, "result": result, "score": score}
                scored_results_by_index[indexing_type].append(scored)
                scored_by_scenario[scenario["id"]][indexing_type] = scored

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=landscape(A4),
        rightMargin=1.0 * cm,
        leftMargin=1.0 * cm,
        topMargin=1.0 * cm,
        bottomMargin=1.0 * cm,
        title="EY RAG Retrieval Evaluation Report",
    )

    story: list[Any] = []
    story.append(paragraph("EY RAG Retrieval Evaluation Report", styles["title"]))
    story.append(
        paragraph(
            f"Run: {results_doc.get('run_name', 'Unnamed run')} | Date: {results_doc.get('run_date', 'N/A')}",
            styles["body"],
        )
    )
    if results_doc.get("notes"):
        story.append(paragraph(results_doc["notes"], styles["body"]))
    story.append(Spacer(1, 0.25 * cm))
    story.append(
        paragraph(
            "Scoring: answer coverage 40, source support 25, image support 20, precision/no hallucination 15. "
            "Human scores in the results file override the displayed score but the auto score remains visible in notes.",
            styles["body"],
        )
    )
    story.append(Spacer(1, 0.35 * cm))
    story.append(build_summary_table(scored_results_by_index, styles))
    story.append(PageBreak())

    for scenario in scenario_list:
        story.append(paragraph(f"{scenario['id']} - {scenario['modality']}", styles["h1"]))
        story.append(paragraph(f"Difficulty: {scenario.get('difficulty', 'N/A')}", styles["small"]))
        story.append(paragraph(f"Retrieval goal: {scenario.get('retrieval_goal', '')}", styles["small"]))
        story.append(Spacer(1, 0.1 * cm))
        story.append(paragraph("Question", styles["h2"]))
        story.append(paragraph(scenario.get("question", ""), styles["body"]))
        story.append(paragraph("Ground Truth", styles["h2"]))
        story.append(paragraph(scenario.get("ground_truth_answer", ""), styles["body"]))

        evidence_rows: list[list[Any]] = [
            [
                paragraph("Type", styles["table_header"]),
                paragraph("Page", styles["table_header"]),
                paragraph("Source", styles["table_header"]),
                paragraph("Notes", styles["table_header"]),
            ]
        ]
        for item in scenario.get("evidence", []):
            evidence_rows.append(
                [
                    paragraph(item.get("type", ""), styles["small"]),
                    paragraph(item.get("page", ""), styles["small"]),
                    paragraph(item.get("source_path", ""), styles["small"]),
                    paragraph(item.get("notes", ""), styles["small"]),
                ]
            )
        evidence_table = Table(evidence_rows, colWidths=[2.0 * cm, 1.5 * cm, 11.0 * cm, 12.0 * cm])
        evidence_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#333333")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D0D0D0")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8F8F8")]),
                ]
            )
        )
        story.append(paragraph("Gold Evidence", styles["h2"]))
        story.append(evidence_table)

        if include_ground_truth_images:
            image_paths = expected_image_sources(scenario)
            if image_paths:
                story.append(Spacer(1, 0.2 * cm))
                image_items = [
                    image_flowable(workspace_root, image_path, 8.0 * cm, 5.5 * cm)
                    for image_path in image_paths
                ]
                story.append(KeepTogether(image_items))

        story.append(Spacer(1, 0.25 * cm))
        result_rows: list[list[Any]] = [
            [
                paragraph("Indexing type", styles["table_header"]),
                paragraph("Score", styles["table_header"]),
                paragraph("Retrieved response", styles["table_header"]),
                paragraph("Retrieved evidence", styles["table_header"]),
                paragraph("Evaluation", styles["table_header"]),
            ]
        ]

        for indexing_type in index_types:
            result = result_lookup.get((scenario["id"], indexing_type))
            score = score_result(scenario, result)
            if result is None:
                answer_text = "No result provided."
                evidence_text = "No retrieved evidence."
            else:
                answer_text = truncate(result.get("retrieved_answer", ""), max_answer_chars)
                image_lines = result.get("retrieved_image_paths") or []
                evidence_text = summarize_contexts(result)
                if image_lines:
                    evidence_text += "\nImages:\n" + "\n".join(str(path) for path in image_lines)
            evidence_cell = retrieved_evidence_cell(
                result,
                evidence_text,
                styles,
                workspace_root,
                max_retrieved_images_per_row,
            )

            score_label = f"{score['display_score']}/100"
            if score["display_score"] != score["auto_score"]:
                score_label += f"\nAuto: {score['auto_score']}/100"

            result_rows.append(
                [
                    paragraph(indexing_type, styles["small"]),
                    paragraph(score_label, styles["small"]),
                    paragraph(answer_text, styles["small"]),
                    evidence_cell,
                    paragraph(score["evaluation_notes"], styles["small"]),
                ]
            )

        result_table = Table(
            result_rows,
            repeatRows=1,
            colWidths=[4.2 * cm, 2.3 * cm, 7.0 * cm, 6.5 * cm, 7.0 * cm],
        )
        result_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#242424")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CFCFCF")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F7F7")]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(paragraph("Retrieval Comparison", styles["h2"]))
        story.append(result_table)
        story.append(PageBreak())

    if story and isinstance(story[-1], PageBreak):
        story.pop()

    doc.build(story, onFirstPage=add_page_number, onLaterPages=add_page_number)


def main() -> None:
    args = parse_args()
    scenarios_doc = load_json(args.scenarios)
    results_doc = load_json(args.results)
    build_report(
        scenarios_doc=scenarios_doc,
        results_doc=results_doc,
        output_path=Path(args.output),
        workspace_root=Path(args.workspace_root).resolve(),
        max_answer_chars=args.max_answer_chars,
        include_ground_truth_images=args.include_ground_truth_images,
        max_retrieved_images_per_row=args.max_retrieved_images_per_row,
    )


if __name__ == "__main__":
    main()
