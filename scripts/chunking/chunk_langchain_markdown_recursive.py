#!/usr/bin/env python3
"""Chunk cleaned Markdown with LangChain header-aware + recursive splitting.

Default input:  output_clean/
Default output: chunks/langchain_markdown_recursive/chunks.jsonl

Install dependency:
    pip install langchain-text-splitters
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
            "Create RAG chunks using LangChain MarkdownHeaderTextSplitter first, "
            "then RecursiveCharacterTextSplitter inside large sections."
        )
    )
    parser.add_argument("--input-dir", default="output_clean", help="Clean Markdown folder.")
    parser.add_argument(
        "--output-dir",
        default="chunks/langchain_markdown_recursive",
        help="Folder for chunks.jsonl and reports.",
    )
    parser.add_argument(
        "--chunk-tokens",
        type=int,
        default=800,
        help="Approximate token target for recursive splitting.",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=100,
        help="Approximate token overlap inside oversized Markdown sections.",
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


def require_langchain_splitters():
    try:
        from langchain_text_splitters import (  # type: ignore
            MarkdownHeaderTextSplitter,
            RecursiveCharacterTextSplitter,
        )
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: langchain-text-splitters\n"
            "Install it with:\n"
            "  venv\\Scripts\\python.exe -m pip install langchain-text-splitters"
        ) from exc
    return MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter


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


def heading_path_from_langchain(metadata: dict, inherited: dict[int, str]) -> list[str]:
    headings = dict(inherited)
    for level in range(1, 7):
        value = metadata.get(f"Header {level}")
        if value:
            headings[level] = str(value)
            for old_level in range(level + 1, 7):
                headings.pop(old_level, None)
    return [headings[level] for level in sorted(headings)]


def heading_path_for_text(text: str, inherited: dict[int, str]) -> list[str]:
    headings = dict(inherited)
    update_headings(headings, text)
    return [headings[level] for level in sorted(headings)]


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
    page: PageRecord,
    heading_path: list[str],
) -> dict[str, object]:
    token_estimate = estimate_tokens(text)
    ctype = content_type(text, page.page_type)
    image_paths = image_paths_for_page(source_path, page.number)
    is_atomic = is_structured_block(text, page.page_type)
    pages = f"{page.number}"
    prefix_parts = [
        f"Document: {source_path.stem}",
        f"Page: {pages}",
        f"Page type: {page.page_type}",
    ]
    if heading_path:
        prefix_parts.append(f"Section: {' > '.join(heading_path)}")
    if ctype != "text":
        prefix_parts.append(f"Content type: {ctype}")
    context_prefix = " | ".join(prefix_parts)
    fingerprint = hashlib.sha256(
        f"{strategy}|{source_path.name}|{page.number}|{sequence}|{text}".encode("utf-8")
    ).hexdigest()[:16]

    return {
        "chunk_id": f"{strategy}:{source_path.stem}:{sequence:06d}:{fingerprint}",
        "strategy": strategy,
        "source_file": str(source_path.resolve()),
        "source_name": source_path.name,
        "page_start": page.number,
        "page_end": page.number,
        "parent_id": parent_id(source_path, page.number, heading_path),
        "section_id": section_id(source_path, page.number, heading_path),
        "chunk_sequence": sequence,
        "retrieval_role": "atomic_block" if is_atomic else "child_chunk",
        "is_atomic": is_atomic,
        "page_type": page.page_type,
        "heading_path": heading_path,
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
        "# LangChain Markdown Recursive Chunking Report",
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
    MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter = require_langchain_splitters()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    include_types = parse_csv_arg(args.include_page_types)
    exclude_types = parse_csv_arg(args.exclude_page_types)

    markdown_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[
            ("#", "Header 1"),
            ("##", "Header 2"),
            ("###", "Header 3"),
            ("####", "Header 4"),
            ("#####", "Header 5"),
            ("######", "Header 6"),
        ],
        strip_headers=False,
    )
    recursive_splitter = RecursiveCharacterTextSplitter(
        chunk_size=args.chunk_tokens,
        chunk_overlap=args.chunk_overlap,
        length_function=estimate_tokens,
        separators=["\n\n", "\n", ". ", "; ", ", ", " ", ""],
    )

    chunks: list[dict[str, object]] = []
    sequence = 0
    files = source_files(input_dir, args.limit)
    for path in files:
        pages = split_pages(path, include_types, exclude_types)
        for page in pages:
            if args.protect_structured_blocks and is_structured_block(page.text, page.page_type):
                sequence += 1
                headings = heading_path_for_text(page.text, page.inherited_headings)
                chunks.append(make_record("langchain_markdown_recursive", path, sequence, page.text, page, headings))
                continue

            header_docs = markdown_splitter.split_text(page.text)
            split_docs = recursive_splitter.split_documents(header_docs)
            for doc in split_docs:
                text = clean_chunk_text(doc.page_content)
                if not text:
                    continue
                sequence += 1
                headings = heading_path_from_langchain(doc.metadata, page.inherited_headings)
                chunks.append(make_record("langchain_markdown_recursive", path, sequence, text, page, headings))

    write_outputs(output_dir, chunks, len(files))
    print(f"Wrote {len(chunks)} chunks to {output_dir / 'chunks.jsonl'}")


if __name__ == "__main__":
    main()
