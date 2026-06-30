"""Reusable markdown cleansing transforms for the EY RAG corpus."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path


PAGE_MARKER_RE = re.compile(r"<!--\s*PAGE\s+(\d+)\s*-->")
PAGE_TYPE_MARKER_RE = re.compile(r"<!--\s*PAGE_TYPE:\s*(\w+)\s*-->")
IMAGE_EXTRACTION_RE = re.compile(
    r"<!-- START IMAGE EXTRACTION \(([^)]+)\) -->\n?(.*?)<!-- END IMAGE EXTRACTION \(\1\) -->",
    re.DOTALL,
)
HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.DOTALL | re.IGNORECASE)
TOC_DOT_LEADER_RE = re.compile(r"\.{4,}")
NUMERIC_HEADING_RE = re.compile(r"^#{1,6}\s+[\d.,\s%€$£+-]+$")
LOOSE_PERCENT_BULLET_RE = re.compile(r"^-\s*[\d.,%]+\s*$")
LOOSE_PERCENT_LINE_RE = re.compile(r"^[\d.,%]+\s*$")
DOUBLE_BULLET_RE = re.compile(r"^-\s*•\s*")
GLUED_CAMEL_RE = re.compile(r"([a-z])([A-Z])")
EMPTY_TABLE_RE = re.compile(
    r"<table>\s*<tr>\s*<th>\s*(?:<br\s*/?>\s*)*\s*</th>\s*</tr>\s*</table>",
    re.IGNORECASE,
)


@dataclass
class CleanStats:
    pages: int = 0
    chars_before: int = 0
    chars_after: int = 0
    extractions_integrated: int = 0
    duplicate_paragraphs_removed: int = 0
    chart_debris_lines_removed: int = 0
    html_tables_converted: int = 0
    html_tables_removed_empty: int = 0
    html_tables_removed_numeric_duplicate: int = 0
    br_tables_split: int = 0
    toc_lines_cleaned: int = 0
    bullets_normalized: int = 0
    false_headings_demoted: int = 0
    incomplete_tables_repaired: int = 0
    page_types: dict[str, int] = field(default_factory=dict)

    def merge(self, other: CleanStats) -> None:
        for key, value in other.__dict__.items():
            if key == "page_types":
                for page_type, count in value.items():
                    self.page_types[page_type] = self.page_types.get(page_type, 0) + count
            else:
                setattr(self, key, getattr(self, key) + value)


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._in_table = True
            self._current_table = []
        elif self._in_table and tag == "tr":
            self._in_row = True
            self._current_row = []
        elif self._in_table and self._in_row and tag in {"td", "th"}:
            self._in_cell = True
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._in_cell:
            cell = _normalize_cell_html("".join(self._cell_parts))
            if self._current_row is not None:
                self._current_row.append(cell)
            self._in_cell = False
            self._cell_parts = []
        elif tag == "tr" and self._in_row:
            if self._current_table is not None and self._current_row is not None:
                self._current_table.append(self._current_row)
            self._in_row = False
            self._current_row = None
        elif tag == "table" and self._in_table:
            if self._current_table is not None:
                self.tables.append(self._current_table)
            self._in_table = False
            self._current_table = None

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._in_cell:
            self._cell_parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._in_cell:
            self._cell_parts.append(f"&#{name};")


def _normalize_cell_html(raw: str) -> str:
    text = raw.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return normalize_whitespace(text)


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_key(text: str) -> str:
    text = text.lower()
    text = text.replace("\ufffd", "")
    text = text.replace("’", "'").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[''`]", "", text)
    text = re.sub(r"(\w)\s+s(\s|$)", r"\1s\2", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def split_pages(content: str) -> list[tuple[int | None, str]]:
    parts = PAGE_MARKER_RE.split(content)
    if not parts:
        return [(None, content)]

    pages: list[tuple[int | None, str]] = []
    if parts[0].strip():
        pages.append((None, parts[0]))

    index = 1
    while index < len(parts):
        page_number = int(parts[index])
        page_body = parts[index + 1] if index + 1 < len(parts) else ""
        pages.append((page_number, page_body))
        index += 2
    return pages


def join_pages(pages: list[tuple[int | None, str]]) -> str:
    chunks: list[str] = []
    for page_number, body in pages:
        if page_number is None:
            chunks.append(body.rstrip("\n"))
            continue
        chunks.append(f"<!-- PAGE {page_number} -->")
        if body:
            chunks.append(body.strip("\n"))
    return "\n\n".join(chunk for chunk in chunks if chunk) + "\n"


def parse_html_tables(table_html: str) -> list[list[list[str]]]:
    parser = _TableHTMLParser()
    parser.feed(table_html)
    parser.close()
    return parser.tables


def table_is_empty(rows: list[list[str]]) -> bool:
    if not rows:
        return True
    joined = " ".join(cell.strip() for row in rows for cell in row)
    return not joined


def table_is_numeric_only(rows: list[list[str]]) -> bool:
    cells = [cell.strip() for row in rows for cell in row if cell.strip()]
    if not cells:
        return True
    return all(re.fullmatch(r"[\d.,%+-]+", cell) for cell in cells)


def table_is_br_chart(rows: list[list[str]]) -> bool:
    if not rows:
        return False
    flat = [cell for row in rows for cell in row if cell.strip()]
    if len(flat) != 1:
        return False
    lines = [line.strip() for line in flat[0].split("\n") if line.strip()]
    return len(lines) >= 4


def rows_to_markdown(rows: list[list[str]], *, headerless: bool = False) -> str:
    if not rows:
        return ""

    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]

    def esc(cell: str) -> str:
        return cell.replace("|", "\\|").replace("\n", "<br>")

    if headerless:
        lines = ["| " + " | ".join(esc(cell) for cell in row) + " |" for row in padded]
        return "\n".join(lines)

    header = padded[0]
    body = padded[1:] if len(padded) > 1 else []
    lines = ["| " + " | ".join(esc(cell) for cell in header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in body:
        lines.append("| " + " | ".join(esc(cell) for cell in row) + " |")
    return "\n".join(lines)


def br_chart_to_markdown(rows: list[list[str]]) -> str:
    lines = [line.strip() for line in rows[0][0].split("\n") if line.strip()]
    if not lines:
        return ""
    return "\n".join(f"- {line}" for line in lines)


def convert_html_table(table_html: str, stats: CleanStats) -> str:
    rows = parse_html_tables(table_html)
    if not rows:
        return table_html
    rows = rows[0]

    if table_is_empty(rows):
        stats.html_tables_removed_empty += 1
        return ""

    if table_is_br_chart(rows):
        stats.br_tables_split += 1
        stats.html_tables_converted += 1
        return br_chart_to_markdown(rows)

    stats.html_tables_converted += 1
    return rows_to_markdown(rows)


def convert_all_html_tables(text: str, stats: CleanStats) -> str:
    def replacer(match: re.Match[str]) -> str:
        converted = convert_html_table(match.group(0), stats)
        return f"\n\n{converted}\n\n" if converted else "\n"

    text = EMPTY_TABLE_RE.sub("\n", text)
    return HTML_TABLE_RE.sub(replacer, text)


def fix_glued_words(text: str) -> str:
    parts = re.split(r"(<!--.*?-->)", text, flags=re.DOTALL)
    fixed: list[str] = []
    for part in parts:
        if part.startswith("<!--") and part.endswith("-->"):
            fixed.append(part)
            continue
        part = GLUED_CAMEL_RE.sub(r"\1 \2", part)
        part = re.sub(r"([a-z])(\d)", r"\1 \2", part)
        fixed.append(part)
    return "".join(fixed)


def clean_toc_lines(text: str, stats: CleanStats) -> str:
    lines: list[str] = []
    for line in text.split("\n"):
        if TOC_DOT_LEADER_RE.search(line):
            cleaned = TOC_DOT_LEADER_RE.sub(" ", line)
            cleaned = re.sub(r"\s+\d+\s*$", "", cleaned.strip())
            cleaned = fix_glued_words(cleaned)
            cleaned = normalize_whitespace(cleaned)
            if cleaned != line.strip():
                stats.toc_lines_cleaned += 1
            lines.append(cleaned)
        else:
            lines.append(line)
    return "\n".join(lines)


def normalize_bullets_and_headings(text: str, stats: CleanStats) -> str:
    lines: list[str] = []
    for line in text.split("\n"):
        original = line
        if DOUBLE_BULLET_RE.match(line):
            line = DOUBLE_BULLET_RE.sub("- ", line)
            stats.bullets_normalized += 1
        if NUMERIC_HEADING_RE.match(line.strip()):
            line = line.strip().lstrip("#").strip()
            stats.false_headings_demoted += 1
        if line != original:
            pass
        lines.append(line)
    return "\n".join(lines)


def is_chart_debris_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if LOOSE_PERCENT_BULLET_RE.match(stripped):
        return True
    if LOOSE_PERCENT_LINE_RE.match(stripped):
        return True
    if NUMERIC_HEADING_RE.match(stripped):
        return True
    if stripped.startswith("|") or stripped.startswith("<table"):
        return False
    if " " not in stripped and len(stripped) > 18 and re.search(r"[a-z][A-Z]", stripped):
        return True
    if len(stripped) < 60 and not stripped.endswith(".") and not stripped.startswith("#"):
        words = stripped.split()
        if 1 <= len(words) <= 3 and all(word[:1].isupper() for word in words if word.isalpha()):
            if not any(word.lower() in {"the", "and", "for", "with", "from", "that", "this", "are", "was"} for word in words):
                return True
    return False


def tail_has_narrative(tail: str) -> bool:
    for paragraph in extract_paragraphs(tail):
        if len(paragraph) >= 100 and re.search(r"[a-z]{3,}", paragraph):
            return True
    return False


def remove_chart_debris_blocks(text: str, stats: CleanStats) -> str:
    if "START IMAGE EXTRACTION" in text:
        return text

    lines = text.split("\n")
    percent_like = sum(
        1
        for line in lines
        if LOOSE_PERCENT_BULLET_RE.match(line.strip())
        or LOOSE_PERCENT_LINE_RE.match(line.strip())
        or NUMERIC_HEADING_RE.match(line.strip())
    )
    if percent_like < 4:
        return text

    kept: list[str] = []
    for line in lines:
        if is_chart_debris_line(line):
            stats.chart_debris_lines_removed += 1
            continue
        kept.append(line)
    return "\n".join(kept)


def paragraph_similarity(left: str, right: str) -> float:
    left_key = normalize_key(left)
    right_key = normalize_key(right)
    if not left_key or not right_key:
        return 0.0
    if left_key in right_key or right_key in left_key:
        return 1.0
    return SequenceMatcher(None, left_key, right_key).ratio()


def extract_paragraphs(text: str) -> list[str]:
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if block.startswith("<!--") or block.startswith("|") or block.startswith("<table"):
            continue
        if len(block) < 40:
            continue
        paragraphs.append(block)

    if len(paragraphs) <= 1:
        for line in text.splitlines():
            line = line.strip()
            if len(line) < 40:
                continue
            if line.startswith("#") or line.startswith("|") or line.startswith("<!--"):
                continue
            if line in {"---", "*EY Parthenon CEO Outlook Survey – January 2025*"}:
                continue
            paragraphs.append(line)

    deduped: list[str] = []
    seen: set[str] = set()
    for paragraph in paragraphs:
        key = normalize_key(paragraph)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(paragraph)
    return deduped


def extraction_has_incomplete_table(extraction: str) -> bool:
    lines = [line.rstrip() for line in extraction.splitlines() if "|" in line]
    if not lines:
        return False
    if any(line.count("|") >= 2 and not line.endswith("|") for line in lines):
        return True

    seen_separator = False
    data_rows = 0
    for line in lines:
        stripped = line.strip()
        if re.match(r"^\|[\s:|-]+\|$", stripped):
            seen_separator = True
            continue
        if seen_separator:
            data_rows += 1
    return seen_separator and data_rows == 0


def find_first_numeric_markdown_table(text: str) -> str | None:
    for match in HTML_TABLE_RE.finditer(text):
        rows = parse_html_tables(match.group(0))
        if rows and table_is_numeric_only(rows[0]):
            return rows_to_markdown(rows[0], headerless=True)
    return None


def dedupe_image_extraction_page(page: str, stats: CleanStats) -> str:
    if "START IMAGE EXTRACTION" not in page:
        return page

    result = page
    for match in reversed(list(IMAGE_EXTRACTION_RE.finditer(result))):
        image_id = match.group(1)
        extraction = match.group(2).strip()
        tail = result[match.end() :]

        if extraction_has_incomplete_table(extraction):
            repaired = find_first_numeric_markdown_table(tail)
            if repaired:
                lines = extraction.splitlines()
                while lines and "|" in lines[-1] and not lines[-1].strip().endswith("|"):
                    lines.pop()
                if lines and "**" in lines[-1]:
                    column_count = lines[-1].count("|") - 1
                    lines.append("| " + " | ".join(["---"] * column_count) + " |")
                extraction = "\n".join(lines).rstrip()
                extraction = f"{extraction}\n{repaired}"
                stats.incomplete_tables_repaired += 1

        reference_paragraphs = extract_paragraphs(extraction)
        cleaned_tail, removed_dupes, removed_debris = _clean_post_extraction_tail(
            tail,
            reference_paragraphs,
            has_extraction=True,
        )
        stats.duplicate_paragraphs_removed += removed_dupes
        stats.chart_debris_lines_removed += removed_debris

        block = (
            f"<!-- START IMAGE EXTRACTION ({image_id}) -->\n"
            f"{extraction}\n"
            f"<!-- END IMAGE EXTRACTION ({image_id}) -->\n"
        )
        result = result[: match.start()] + block + cleaned_tail
    return result


def is_duplicate_fragment(block: str, reference_paragraphs: list[str]) -> bool:
    block_key = normalize_key(block)
    if len(block_key) < 25:
        return False
    for reference in reference_paragraphs:
        ref_key = normalize_key(reference)
        if block_key in ref_key or ref_key in block_key:
            return True
        if paragraph_similarity(block, reference) >= 0.72:
            return True
    return False


def split_tail_blocks(tail: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\n\s*\n", tail) if part.strip()]
    if len(parts) > 1:
        return parts

    blocks: list[str] = []
    current: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("<!-- PAGE "):
            if current:
                blocks.append("\n".join(current))
                current = []
            blocks.append(stripped)
            continue
        current.append(stripped)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _clean_post_extraction_tail(
    tail: str,
    reference_paragraphs: list[str],
    has_extraction: bool,
) -> tuple[str, int, int]:
    removed_dupes = 0
    removed_debris = 0
    kept: list[str] = []

    for block_stripped in split_tail_blocks(tail):
        if not block_stripped:
            continue

        if block_stripped.startswith("<!-- PAGE "):
            kept.append(block_stripped)
            continue

        if HTML_TABLE_RE.fullmatch(block_stripped) or block_stripped.startswith("<table"):
            rows = parse_html_tables(block_stripped)
            if rows and table_is_numeric_only(rows[0]) and has_extraction:
                removed_debris += 1
                continue

        if any(paragraph_similarity(block_stripped, reference) >= 0.82 for reference in reference_paragraphs):
            removed_dupes += 1
            continue

        if is_duplicate_fragment(block_stripped, reference_paragraphs):
            removed_dupes += 1
            continue

        if has_extraction:
            line_list = block_stripped.splitlines()
            filtered_lines = []
            for line in line_list:
                if is_chart_debris_line(line):
                    removed_debris += 1
                    continue
                filtered_lines.append(line)
            block_stripped = "\n".join(filtered_lines).strip()
            if not block_stripped:
                continue

        kept.append(block_stripped)

    result = "\n\n".join(kept)
    if has_extraction and not tail_has_narrative(result):
        marker_only = [block for block in kept if block.startswith("<!-- PAGE ")]
        result = "\n\n".join(marker_only)
    return result, removed_dupes, removed_debris


def classify_page_type(page_number: int | None, page: str) -> str:
    lowered = page.lower()
    if "start image extraction" in lowered:
        return "image_extraction"
    if page_number == 1 and len(page.strip()) < 1200:
        return "cover"
    if re.search(r"^#{1,6}\s*contents\b", page, re.IGNORECASE | re.MULTILINE):
        return "toc"
    if "ey offices" in lowered or "building a better working world" in lowered:
        return "boilerplate"
    if len(normalize_key(page)) < 80:
        return "sparse"
    return "content"


def tag_page(page_number: int | None, page: str) -> tuple[str, str]:
    page_type = classify_page_type(page_number, page)
    if PAGE_TYPE_MARKER_RE.search(page):
        page = PAGE_TYPE_MARKER_RE.sub(f"<!-- PAGE_TYPE: {page_type} -->", page, count=1)
    else:
        page = f"<!-- PAGE_TYPE: {page_type} -->\n{page.lstrip()}"
    return page, page_type


def trim_page_after_last_extraction(page: str, stats: CleanStats) -> str:
    last_end: int | None = None
    for match in IMAGE_EXTRACTION_RE.finditer(page):
        last_end = match.end()
    if last_end is None:
        return page

    tail = page[last_end:].strip()
    if not tail:
        return page
    if tail.startswith("<!-- PAGE "):
        return page[:last_end] + "\n\n" + tail

    stats.duplicate_paragraphs_removed += len(split_tail_blocks(tail))
    return page[:last_end] + "\n"


def clean_page_body(page: str, stats: CleanStats) -> str:
    page = remove_chart_debris_blocks(page, stats)
    page = dedupe_image_extraction_page(page, stats)
    page = convert_all_html_tables(page, stats)
    page = trim_page_after_last_extraction(page, stats)
    page = clean_toc_lines(page, stats)
    page = normalize_bullets_and_headings(page, stats)
    page = fix_glued_words(page)
    page = normalize_whitespace(page)
    return page


def integrate_cache_extractions(
    content: str,
    document: str,
    cache: dict[str, str],
    stats: CleanStats,
) -> str:
    for filename, extracted_text in sorted(cache.items()):
        if not filename.startswith(f"{document}__"):
            continue

        parts = Path(filename).stem.split("__")
        if len(parts) < 3:
            continue
        image_id = parts[2]
        marker = f"<!-- START IMAGE EXTRACTION ({image_id}) -->"
        if marker in content:
            continue

        pattern = re.compile(
            r"!\[image\s+\d+\]\(<[^>]+_images/" + re.escape(image_id) + r"\.png>\)",
            re.IGNORECASE,
        )
        formatted = (
            f"\n<!-- START IMAGE EXTRACTION ({image_id}) -->\n"
            f"{extracted_text.strip()}\n"
            f"<!-- END IMAGE EXTRACTION ({image_id}) -->\n"
        )
        if pattern.search(content):
            content = pattern.sub(formatted, content, count=1)
            stats.extractions_integrated += 1
            continue

        page_token = parts[1]
        page_match = re.match(r"p(\d+)", page_token, re.IGNORECASE)
        if not page_match:
            continue
        page_number = int(page_match.group(1))
        page_marker = f"<!-- PAGE {page_number} -->"
        if page_marker not in content:
            continue

        page_header = re.compile(
            rf"({re.escape(page_marker)}\s*(?:\n<!-- PAGE_TYPE: \w+ -->)?\s*)"
        )
        if page_header.search(content):
            content = page_header.sub(r"\1" + formatted, content, count=1)
            stats.extractions_integrated += 1
    return content


def strip_remaining_image_refs(content: str) -> str:
    pattern = re.compile(r"!\[image\s+\d+\]\(<[^>]+_images/imageFile\d+\.png>\)", re.IGNORECASE)
    return pattern.sub("", content)


def clean_document(
    content: str,
    document: str,
    cache: dict[str, str] | None = None,
) -> tuple[str, CleanStats]:
    stats = CleanStats()
    stats.chars_before = len(content)

    if cache:
        content = integrate_cache_extractions(content, document, cache, stats)
    content = strip_remaining_image_refs(content)

    pages = split_pages(content)
    cleaned_pages: list[tuple[int | None, str]] = []
    for page_number, body in pages:
        stats.pages += 1
        cleaned_body = clean_page_body(body, stats)
        cleaned_body, page_type = tag_page(page_number, cleaned_body)
        stats.page_types[page_type] = stats.page_types.get(page_type, 0) + 1
        cleaned_pages.append((page_number, cleaned_body))

    result = join_pages(cleaned_pages)
    stats.chars_after = len(result)
    return result, stats
