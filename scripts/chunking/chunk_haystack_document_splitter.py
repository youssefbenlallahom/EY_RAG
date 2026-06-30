#!/usr/bin/env python3
"""Chunk cleaned Markdown with Haystack DocumentSplitter.

Default input:  output_clean/
Default output: chunks/haystack_document_splitter/chunks.jsonl

Install dependency:
    pip install haystack-ai
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PAGE_MARKER_RE = re.compile(r"<!--\s*PAGE\s+(\d+)\s*-->")
PAGE_TYPE_MARKER_RE = re.compile(r"<!--\s*PAGE_TYPE:\s*([a-zA-Z0-9_-]+)\s*-->")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
IMAGE_BLOCK_RE = re.compile(
    r"<!--\s*START IMAGE EXTRACTION \(([^)]+)\)\s*-->.*?"
    r"<!--\s*END IMAGE EXTRACTION \(\1\)\s*-->",
    re.DOTALL,
)
IMAGE_COMMENT_RE = re.compile(
    r"<!--\s*(?:START|END) IMAGE EXTRACTION \([^)]+\)\s*-->"
)
MD_TABLE_RE = re.compile(r"^\|.*\|\s*\n^\|[\s:|.-]+\|\s*$", re.MULTILINE)
STANDARD_REF_RE = re.compile(
    r"\b(?:IFRS|IAS|ISA|ISQM|ISAE|ISRE|ISRS|IFRIC|SIC)\s*"
    r"\d+[A-Z]?(?:\.[A-Z]?\d+[A-Z]?)*\b"
)
REPORT_STEMS = {
    "markdown_clean_report",
    "image_description_rag_quality_audit",
    "rag_extraction_quality_report",
    "rag_extraction_quality_summary",
}


@dataclass
class PageRecord:
    number: int
    page_type: str
    text: str
    inherited_headings: dict[int, str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create RAG chunks using Haystack DocumentSplitter. "
            "This is a production-style baseline with consistent metadata."
        )
    )
    parser.add_argument("--input-dir", default="output_clean", help="Clean Markdown folder.")
    parser.add_argument(
        "--output-dir",
        default="chunks/haystack_document_splitter",
        help="Folder for chunks.jsonl and reports.",
    )
    parser.add_argument(
        "--split-by",
        choices=["word", "sentence", "passage", "page", "line"],
        default="word",
        help="Haystack split unit.",
    )
    parser.add_argument(
        "--split-length",
        type=int,
        default=550,
        help="Chunk length in the unit chosen by --split-by.",
    )
    parser.add_argument(
        "--split-overlap",
        type=int,
        default=75,
        help="Overlap in the unit chosen by --split-by.",
    )
    parser.add_argument(
        "--split-threshold",
        type=int,
        default=100,
        help="Small trailing splits shorter than this are attached to the previous split.",
    )
    parser.add_argument(
        "--protect-structured-blocks",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep image extraction pages and Markdown tables as atomic chunks.",
    )
    parser.add_argument(
        "--include-page-types",
        default="content,image_extraction,sparse",
        help="Comma-separated page types to chunk, or 'all'.",
    )
    parser.add_argument(
        "--exclude-page-types",
        default="cover,toc,boilerplate",
        help="Comma-separated page types to skip.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process first N files only.")
    return parser.parse_args()


def require_haystack():
    try:
        from haystack import Document  # type: ignore
        from haystack.components.preprocessors import DocumentSplitter  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: haystack-ai\n"
            "Install it with:\n"
            "  venv\\Scripts\\python.exe -m pip install haystack-ai"
        ) from exc
    return Document, DocumentSplitter


def parse_csv_arg(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def source_files(input_dir: Path, limit: int | None) -> list[Path]:
    files = [
        path
        for path in sorted(input_dir.glob("*.md"))
        if path.stem not in REPORT_STEMS and not path.name.endswith("_report.md")
    ]
    return files[:limit] if limit else files


def clean_chunk_text(text: str) -> str:
    text = PAGE_TYPE_MARKER_RE.sub("", text)
    text = IMAGE_COMMENT_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def estimate_tokens(text: str) -> int:
    words = re.findall(r"\S+", text)
    return max(1, int(len(words) * 1.3))


def update_headings(active: dict[int, str], text: str) -> None:
    for line in text.splitlines():
        match = HEADING_RE.match(line.strip())
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        active[level] = title
        for old_level in range(level + 1, 7):
            active.pop(old_level, None)


def heading_path(active: dict[int, str]) -> list[str]:
    return [active[level] for level in sorted(active)]


def heading_path_for_text(text: str, inherited: dict[int, str]) -> list[str]:
    active = dict(inherited)
    update_headings(active, text)
    return heading_path(active)


def split_pages(path: Path, include_types: set[str], exclude_types: set[str]) -> list[PageRecord]:
    raw = path.read_text(encoding="utf-8")
    matches = list(PAGE_MARKER_RE.finditer(raw))
    active_headings: dict[int, str] = {}
    pages: list[PageRecord] = []

    if not matches:
        text = clean_chunk_text(raw)
        if text:
            pages.append(PageRecord(1, "content", text, {}))
        return pages

    for index, match in enumerate(matches):
        page_number = int(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        segment = raw[start:end]
        type_match = PAGE_TYPE_MARKER_RE.search(segment)
        page_type = type_match.group(1).lower() if type_match else "content"
        text = clean_chunk_text(segment)
        inherited = dict(active_headings)
        update_headings(active_headings, text)

        if not text:
            continue
        if page_type in exclude_types:
            continue
        if include_types != {"all"} and page_type not in include_types:
            continue
        pages.append(PageRecord(page_number, page_type, text, inherited))

    return pages


def content_type(text: str, page_type: str) -> str:
    has_image = bool(IMAGE_BLOCK_RE.search(text) or "IMAGE EXTRACTION" in text)
    has_image = has_image or page_type == "image_extraction"
    has_table = bool(MD_TABLE_RE.search(text))
    if has_image and has_table:
        return "figure_table"
    if has_image:
        return "figure"
    if has_table:
        return "table"
    return "text"


def image_paths_for_page(source_path: Path, page_number: int) -> list[str]:
    image_dir = Path("filtered_images")
    if not image_dir.exists():
        return []
    return [match.as_posix() for match in sorted(image_dir.glob(f"{source_path.stem}__p{page_number}__*.png"))]


def is_structured_block(text: str, page_type: str) -> bool:
    ctype = content_type(text, page_type)
    return page_type == "image_extraction" or ctype in {"figure", "figure_table", "table"}


def section_id(source_path: Path, page_number: int, headings: list[str]) -> str:
    heading_key = " > ".join(headings) if headings else "page"
    digest = hashlib.sha256(f"{source_path.name}|{page_number}|{heading_key}".encode("utf-8")).hexdigest()[:12]
    return f"{source_path.stem}:p{page_number}:{digest}"


def parent_id(source_path: Path, page_number: int, headings: list[str]) -> str:
    digest = hashlib.sha256(
        f"{source_path.name}|{page_number}|{' > '.join(headings)}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{source_path.stem}:p{page_number}:{digest}"


def add_window_metadata(chunks: list[dict[str, object]]) -> None:
    grouped: dict[str, list[dict[str, object]]] = {}
    for chunk in chunks:
        key = str(chunk.get("parent_id") or f"{chunk.get('source_name')}:{chunk.get('page_start')}")
        grouped.setdefault(key, []).append(chunk)
    for group in grouped.values():
        group.sort(key=lambda item: int(item.get("chunk_sequence") or 0))
        for index, chunk in enumerate(group):
            chunk["previous_chunk_id"] = str(group[index - 1].get("chunk_id")) if index > 0 else ""
            chunk["next_chunk_id"] = str(group[index + 1].get("chunk_id")) if index + 1 < len(group) else ""


def standard_refs(text: str) -> list[str]:
    refs = {re.sub(r"\s+", " ", match.group(0)).strip() for match in STANDARD_REF_RE.finditer(text)}
    return sorted(refs)


def make_record(
    strategy: str,
    source_path: Path,
    sequence: int,
    text: str,
    page_number: int,
    page_type: str,
    headings: list[str],
) -> dict[str, object]:
    token_estimate = estimate_tokens(text)
    ctype = content_type(text, page_type)
    image_paths = image_paths_for_page(source_path, page_number)
    is_atomic = is_structured_block(text, page_type)
    prefix_parts = [
        f"Document: {source_path.stem}",
        f"Page: {page_number}",
        f"Page type: {page_type}",
    ]
    if headings:
        prefix_parts.append(f"Section: {' > '.join(headings)}")
    if ctype != "text":
        prefix_parts.append(f"Content type: {ctype}")
    context_prefix = " | ".join(prefix_parts)
    fingerprint = hashlib.sha256(
        f"{strategy}|{source_path.name}|{page_number}|{sequence}|{text}".encode("utf-8")
    ).hexdigest()[:16]

    return {
        "chunk_id": f"{strategy}:{source_path.stem}:{sequence:06d}:{fingerprint}",
        "strategy": strategy,
        "source_file": str(source_path.resolve()),
        "source_name": source_path.name,
        "page_start": page_number,
        "page_end": page_number,
        "parent_id": parent_id(source_path, page_number, headings),
        "section_id": section_id(source_path, page_number, headings),
        "chunk_sequence": sequence,
        "retrieval_role": "atomic_block" if is_atomic else "child_chunk",
        "is_atomic": is_atomic,
        "page_type": page_type,
        "heading_path": headings,
        "content_type": ctype,
        "image_paths": image_paths,
        "primary_image_path": image_paths[0] if image_paths else "",
        "standard_refs": standard_refs(text),
        "char_count": len(text),
        "token_estimate": token_estimate,
        "context_prefix": context_prefix,
        "text": text,
        "text_for_embedding": f"{context_prefix}\n\n{text}",
    }


def write_outputs(output_dir: Path, chunks: list[dict[str, object]], docs_seen: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    add_window_metadata(chunks)
    jsonl_path = output_dir / "chunks.jsonl"
    csv_path = output_dir / "chunk_summary.csv"
    md_path = output_dir / "chunking_report.md"

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")

    fieldnames = [
        "chunk_id",
        "strategy",
        "source_name",
        "page_start",
        "page_end",
        "page_type",
        "parent_id",
        "section_id",
        "retrieval_role",
        "is_atomic",
        "content_type",
        "primary_image_path",
        "token_estimate",
        "char_count",
        "heading_path",
        "standard_refs",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(chunks)

    token_values = [int(chunk["token_estimate"]) for chunk in chunks]
    type_counts = Counter(str(chunk["content_type"]) for chunk in chunks)
    doc_counts = Counter(str(chunk["source_name"]) for chunk in chunks)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# Haystack DocumentSplitter Chunking Report",
        "",
        f"Generated: {generated}",
        "",
        "## Summary",
        "",
        f"- Documents processed: {docs_seen}",
        f"- Chunks written: {len(chunks)}",
        f"- Output JSONL: `{jsonl_path}`",
        f"- Output CSV: `{csv_path}`",
    ]
    if token_values:
        lines.extend(
            [
                f"- Min tokens: {min(token_values)}",
                f"- Avg tokens: {round(sum(token_values) / len(token_values), 1)}",
                f"- Max tokens: {max(token_values)}",
            ]
        )
    lines.extend(["", "## Content Types", ""])
    for name, count in sorted(type_counts.items()):
        lines.append(f"- {name}: {count}")
    lines.extend(["", "## Per Document", ""])
    for name, count in sorted(doc_counts.items()):
        lines.append(f"- {name}: {count}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    Document, DocumentSplitter = require_haystack()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    include_types = parse_csv_arg(args.include_page_types)
    exclude_types = parse_csv_arg(args.exclude_page_types)

    documents = []
    chunks: list[dict[str, object]] = []
    sequence = 0
    files = source_files(input_dir, args.limit)
    for path in files:
        for page in split_pages(path, include_types, exclude_types):
            if args.protect_structured_blocks and is_structured_block(page.text, page.page_type):
                sequence += 1
                chunks.append(
                    make_record(
                        "haystack_document_splitter",
                        path,
                        sequence,
                        page.text,
                        page.number,
                        page.page_type,
                        heading_path_for_text(page.text, page.inherited_headings),
                    )
                )
                continue

            documents.append(
                Document(
                    content=page.text,
                    meta={
                        "source_name": path.name,
                        "source_file": str(path.resolve()),
                        "source_page": page.number,
                        "page_type": page.page_type,
                        "inherited_headings_json": json.dumps(page.inherited_headings),
                    },
                )
            )

    splitter = DocumentSplitter(
        split_by=args.split_by,
        split_length=args.split_length,
        split_overlap=args.split_overlap,
        split_threshold=args.split_threshold,
    )
    result = splitter.run(documents=documents)
    split_docs = result["documents"]

    for document in split_docs:
        text = clean_chunk_text(document.content or "")
        if not text:
            continue
        sequence += 1
        metadata = dict(document.meta or {})
        source_path = Path(str(metadata.get("source_file") or ""))
        if not source_path.exists():
            source_path = input_dir / str(metadata.get("source_name") or "unknown.md")
        page_number = int(metadata.get("source_page") or metadata.get("page_number") or 1)
        page_type = str(metadata.get("page_type") or "content")
        inherited_raw = metadata.get("inherited_headings_json") or "{}"
        inherited = {int(k): str(v) for k, v in json.loads(str(inherited_raw)).items()}
        headings = heading_path_for_text(text, inherited)
        chunks.append(
            make_record(
                "haystack_document_splitter",
                source_path,
                sequence,
                text,
                page_number,
                page_type,
                headings,
            )
        )

    write_outputs(output_dir, chunks, len(files))
    print(f"Wrote {len(chunks)} chunks to {output_dir / 'chunks.jsonl'}")


if __name__ == "__main__":
    main()
