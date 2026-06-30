"""Shared helpers for RAG chunking scripts."""

from __future__ import annotations

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


@dataclass
class PageSpan:
    number: int
    page_type: str
    text: str
    normalized_text: str


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
    text = PAGE_MARKER_RE.sub("", text)
    text = PAGE_TYPE_MARKER_RE.sub("", text)
    text = IMAGE_COMMENT_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_for_search(text: str) -> str:
    text = clean_chunk_text(text).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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


def heading_path_for_text(text: str, inherited: dict[int, str] | None = None) -> list[str]:
    active = dict(inherited or {})
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


class PageLocator:
    """Approximate source-page lookup for chunkers that return transformed text."""

    def __init__(self, source_path: Path) -> None:
        self.pages = self._load_pages(source_path)

    def _load_pages(self, source_path: Path) -> list[PageSpan]:
        raw = source_path.read_text(encoding="utf-8")
        matches = list(PAGE_MARKER_RE.finditer(raw))
        if not matches:
            text = clean_chunk_text(raw)
            return [PageSpan(1, "content", text, normalize_for_search(text))]

        pages: list[PageSpan] = []
        for index, match in enumerate(matches):
            page_number = int(match.group(1))
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            segment = raw[start:end]
            type_match = PAGE_TYPE_MARKER_RE.search(segment)
            page_type = type_match.group(1).lower() if type_match else "content"
            text = clean_chunk_text(segment)
            pages.append(PageSpan(page_number, page_type, text, normalize_for_search(text)))
        return pages or [PageSpan(1, "content", clean_chunk_text(raw), normalize_for_search(raw))]

    def locate(self, text: str) -> tuple[int, int, str]:
        normalized = normalize_for_search(text)
        if not normalized:
            return 1, 1, "content"

        words = normalized.split()
        snippets = set()
        if len(words) <= 24:
            snippets.add(" ".join(words))
        else:
            snippets.add(" ".join(words[:18]))
            mid = max(0, len(words) // 2 - 9)
            snippets.add(" ".join(words[mid : mid + 18]))
            snippets.add(" ".join(words[-18:]))

        hits: list[PageSpan] = []
        for page in self.pages:
            if normalized and normalized in page.normalized_text:
                hits.append(page)
                continue
            if any(snippet and snippet in page.normalized_text for snippet in snippets):
                hits.append(page)

        if not hits:
            return 1, 1, "content"

        page_start = min(page.number for page in hits)
        page_end = max(page.number for page in hits)
        page_types = sorted({page.page_type for page in hits})
        return page_start, page_end, ",".join(page_types)


def content_type(text: str, page_type: str = "") -> str:
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


def image_paths_for_pages(
    source_path: Path,
    page_start: int,
    page_end: int | None = None,
    filtered_images_dir: Path | str = "filtered_images",
) -> list[str]:
    """Return filtered-image files that correspond to a source page span."""
    image_dir = Path(filtered_images_dir)
    if not image_dir.exists():
        return []

    end = page_start if page_end is None else page_end
    paths: list[str] = []
    seen: set[str] = set()
    for page_number in range(page_start, end + 1):
        pattern = f"{source_path.stem}__p{page_number}__*.png"
        for match in sorted(image_dir.glob(pattern)):
            value = match.as_posix()
            if value not in seen:
                seen.add(value)
                paths.append(value)
    return paths


def is_structured_block(text: str, page_type: str = "") -> bool:
    """Keep figure/table-like pages whole unless the caller explicitly splits them."""
    ctype = content_type(text, page_type)
    return page_type == "image_extraction" or ctype in {"figure", "figure_table", "table"}


def section_id(source_path: Path, page_start: int, headings: list[str]) -> str:
    heading_key = " > ".join(headings) if headings else "page"
    digest = hashlib.sha256(
        f"{source_path.name}|{page_start}|{heading_key}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{source_path.stem}:p{page_start}:{digest}"


def parent_id(source_path: Path, page_start: int, page_end: int, headings: list[str]) -> str:
    pages = f"p{page_start}" if page_start == page_end else f"p{page_start}-{page_end}"
    digest = hashlib.sha256(
        f"{source_path.name}|{page_start}|{page_end}|{' > '.join(headings)}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{source_path.stem}:{pages}:{digest}"


def standard_refs(text: str) -> list[str]:
    refs = {re.sub(r"\s+", " ", match.group(0)).strip() for match in STANDARD_REF_RE.finditer(text)}
    return sorted(refs)


def make_record(
    strategy: str,
    source_path: Path,
    sequence: int,
    text: str,
    page_start: int,
    page_end: int,
    page_type: str,
    headings: list[str],
    technique_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    token_estimate = estimate_tokens(text)
    ctype = content_type(text, page_type)
    image_paths = image_paths_for_pages(source_path, page_start, page_end)
    is_atomic = is_structured_block(text, page_type)
    pages = f"{page_start}" if page_start == page_end else f"{page_start}-{page_end}"
    prefix_parts = [
        f"Document: {source_path.stem}",
        f"Page: {pages}",
        f"Page type: {page_type}",
    ]
    if headings:
        prefix_parts.append(f"Section: {' > '.join(headings)}")
    if ctype != "text":
        prefix_parts.append(f"Content type: {ctype}")
    context_prefix = " | ".join(prefix_parts)
    fingerprint = hashlib.sha256(
        f"{strategy}|{source_path.name}|{page_start}|{sequence}|{text}".encode("utf-8")
    ).hexdigest()[:16]

    return {
        "chunk_id": f"{strategy}:{source_path.stem}:{sequence:06d}:{fingerprint}",
        "strategy": strategy,
        "source_file": str(source_path.resolve()),
        "source_name": source_path.name,
        "page_start": page_start,
        "page_end": page_end,
        "parent_id": parent_id(source_path, page_start, page_end, headings),
        "section_id": section_id(source_path, page_start, headings),
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
        "technique_metadata": technique_metadata or {},
    }


def write_outputs(
    output_dir: Path,
    chunks: list[dict[str, object]],
    docs_seen: int,
    report_title: str,
) -> None:
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
        "technique_metadata",
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
        f"# {report_title}",
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
