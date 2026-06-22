import argparse
import csv
import hashlib
import json
import math
import os
import struct
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


@dataclass
class ImageMetadata:
    document: str | None = None
    page_number: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    pdfua_tag: str | None = None


@dataclass
class PageMetadata:
    bbox: tuple[float, float, float, float] | None = None
    text_chars: int = 0


@dataclass
class ImageStats:
    width: int | None = None
    height: int | None = None
    bit_depth: int | None = None
    color_type: int | None = None
    pixel_sample_count: int = 0
    unique_sample_colors: int | None = None
    dominant_color_ratio: float | None = None
    white_or_transparent_ratio: float | None = None
    transparent_ratio: float | None = None
    luminance_stddev: float | None = None
    decode_error: str | None = None


@dataclass
class ImageRecord:
    path: Path
    rel_path: str
    size_bytes: int
    sha256: str
    metadata: ImageMetadata
    stats: ImageStats
    score: int = 0
    decision: str = "candidate"
    reasons: list[str] = field(default_factory=list)
    bbox_width: float | None = None
    bbox_height: float | None = None
    bbox_area_ratio: float | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cheap, non-destructive decorative image pre-filter for extracted PDF images. "
            "Writes the CSV manifest consumed by the OCR filter."
        )
    )
    parser.add_argument("--output-dir", default="output", help="Folder containing extracted markdown/json/images.")
    parser.add_argument(
        "--manifest-prefix",
        default="image_prefilter_manifest",
        help="Manifest base name written inside output-dir.",
    )
    return parser.parse_args()


def normalize_rel_path(path: str) -> str:
    return Path(path.replace("/", os.sep)).as_posix()


def walk_json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json_objects(child)
    elif isinstance(value, list):
        for item in value:
            yield from walk_json_objects(item)


def bbox_from_value(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return x1, y1, x2, y2


def merge_bbox(
    first: tuple[float, float, float, float] | None,
    second: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    if first is None:
        return second
    if second is None:
        return first
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def load_extraction_metadata(output_dir: Path) -> tuple[dict[str, ImageMetadata], dict[tuple[str, int], PageMetadata]]:
    image_metadata: dict[str, ImageMetadata] = {}
    page_metadata: dict[tuple[str, int], PageMetadata] = defaultdict(PageMetadata)

    for json_path in sorted(output_dir.glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Warning: could not read metadata {json_path}: {exc}")
            continue

        document = json_path.stem
        for obj in walk_json_objects(data):
            page = obj.get("page number")
            try:
                page_number = int(page) if page is not None else None
            except (TypeError, ValueError):
                page_number = None

            bbox = bbox_from_value(obj.get("bounding box"))
            if page_number is not None:
                key = (document, page_number)
                page_metadata[key].bbox = merge_bbox(page_metadata[key].bbox, bbox)

            content = obj.get("content")
            if page_number is not None and isinstance(content, str):
                page_metadata[(document, page_number)].text_chars += len(content.strip())

            if obj.get("type") == "image" and isinstance(obj.get("source"), str):
                rel_source = normalize_rel_path(obj["source"])
                image_metadata[rel_source] = ImageMetadata(
                    document=document,
                    page_number=page_number,
                    bbox=bbox,
                    pdfua_tag=obj.get("pdfua_tag"),
                )

    return image_metadata, page_metadata


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_png_stats(path: Path, sample_limit: int = 6000, max_sample_pixels: int = 350_000) -> ImageStats:
    stats = ImageStats()
    try:
        data = path.read_bytes()
    except OSError as exc:
        stats.decode_error = str(exc)
        return stats

    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        stats.decode_error = "not_png"
        return stats

    pos = 8
    idat_parts: list[bytes] = []
    palette: list[tuple[int, int, int]] = []

    try:
        while pos + 8 <= len(data):
            length = struct.unpack(">I", data[pos : pos + 4])[0]
            chunk_type = data[pos + 4 : pos + 8]
            chunk_data = data[pos + 8 : pos + 8 + length]
            pos += 12 + length

            if chunk_type == b"IHDR":
                (
                    stats.width,
                    stats.height,
                    stats.bit_depth,
                    stats.color_type,
                    _compression,
                    _filter_method,
                    interlace,
                ) = struct.unpack(">IIBBBBB", chunk_data)
                if interlace != 0:
                    stats.decode_error = "interlaced_png_not_sampled"
                    return stats
            elif chunk_type == b"PLTE":
                palette = [
                    tuple(chunk_data[index : index + 3])
                    for index in range(0, len(chunk_data) - 2, 3)
                ]
            elif chunk_type == b"IDAT":
                idat_parts.append(chunk_data)
            elif chunk_type == b"IEND":
                break
    except (struct.error, ValueError) as exc:
        stats.decode_error = f"png_parse_error:{exc}"
        return stats

    if not stats.width or not stats.height:
        stats.decode_error = "missing_png_dimensions"
        return stats
    if stats.width * stats.height > max_sample_pixels:
        stats.decode_error = "pixel_sampling_skipped_large_png"
        return stats
    if stats.bit_depth != 8:
        stats.decode_error = f"unsupported_bit_depth:{stats.bit_depth}"
        return stats
    if stats.color_type not in {0, 2, 3, 4, 6}:
        stats.decode_error = f"unsupported_color_type:{stats.color_type}"
        return stats

    channels_by_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    channels = channels_by_type[stats.color_type]
    row_len = stats.width * channels

    try:
        raw = zlib.decompress(b"".join(idat_parts))
    except zlib.error as exc:
        stats.decode_error = f"png_zlib_error:{exc}"
        return stats

    expected_len = (row_len + 1) * stats.height
    if len(raw) < expected_len:
        stats.decode_error = "truncated_png_data"
        return stats

    def paeth(a: int, b: int, c: int) -> int:
        p = a + b - c
        pa = abs(p - a)
        pb = abs(p - b)
        pc = abs(p - c)
        if pa <= pb and pa <= pc:
            return a
        if pb <= pc:
            return b
        return c

    sample_step = max(1, int(math.sqrt((stats.width * stats.height) / sample_limit)))
    colors: Counter[tuple[int, int, int, int]] = Counter()
    luminance_values: list[float] = []
    white_or_transparent = 0
    transparent = 0
    sample_count = 0

    previous = bytearray(row_len)
    cursor = 0
    try:
        for y in range(stats.height):
            filter_type = raw[cursor]
            cursor += 1
            row = bytearray(raw[cursor : cursor + row_len])
            cursor += row_len

            for index in range(row_len):
                left = row[index - channels] if index >= channels else 0
                up = previous[index]
                up_left = previous[index - channels] if index >= channels else 0
                if filter_type == 1:
                    row[index] = (row[index] + left) & 0xFF
                elif filter_type == 2:
                    row[index] = (row[index] + up) & 0xFF
                elif filter_type == 3:
                    row[index] = (row[index] + ((left + up) // 2)) & 0xFF
                elif filter_type == 4:
                    row[index] = (row[index] + paeth(left, up, up_left)) & 0xFF
                elif filter_type != 0:
                    stats.decode_error = f"unknown_png_filter:{filter_type}"
                    return stats

            if y % sample_step == 0:
                for x in range(0, stats.width, sample_step):
                    offset = x * channels
                    if stats.color_type == 0:
                        r = g = b = row[offset]
                        alpha = 255
                    elif stats.color_type == 2:
                        r, g, b = row[offset], row[offset + 1], row[offset + 2]
                        alpha = 255
                    elif stats.color_type == 3:
                        palette_index = row[offset]
                        if palette_index < len(palette):
                            r, g, b = palette[palette_index]
                        else:
                            r = g = b = 0
                        alpha = 255
                    elif stats.color_type == 4:
                        r = g = b = row[offset]
                        alpha = row[offset + 1]
                    else:
                        r, g, b = row[offset], row[offset + 1], row[offset + 2]
                        alpha = row[offset + 3]

                    colors[(r, g, b, alpha)] += 1
                    luminance_values.append(0.2126 * r + 0.7152 * g + 0.0722 * b)
                    is_transparent = alpha <= 10
                    if is_transparent:
                        transparent += 1
                    if is_transparent or (r >= 245 and g >= 245 and b >= 245):
                        white_or_transparent += 1
                    sample_count += 1

            previous = row
    except IndexError as exc:
        stats.decode_error = f"png_decode_error:{exc}"
        return stats

    stats.pixel_sample_count = sample_count
    if sample_count:
        stats.unique_sample_colors = len(colors)
        stats.dominant_color_ratio = max(colors.values()) / sample_count
        stats.white_or_transparent_ratio = white_or_transparent / sample_count
        stats.transparent_ratio = transparent / sample_count
        mean = sum(luminance_values) / sample_count
        variance = sum((value - mean) ** 2 for value in luminance_values) / sample_count
        stats.luminance_stddev = math.sqrt(variance)
    return stats


def read_jpeg_dimensions(path: Path) -> ImageStats:
    stats = ImageStats(decode_error="jpeg_pixels_not_sampled")
    try:
        data = path.read_bytes()
    except OSError as exc:
        stats.decode_error = str(exc)
        return stats

    if not data.startswith(b"\xff\xd8"):
        stats.decode_error = "unsupported_image_format"
        return stats

    pos = 2
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker in {0xD8, 0xD9}:
            continue
        if pos + 2 > len(data):
            break
        segment_len = struct.unpack(">H", data[pos : pos + 2])[0]
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            stats.height = struct.unpack(">H", data[pos + 3 : pos + 5])[0]
            stats.width = struct.unpack(">H", data[pos + 5 : pos + 7])[0]
            return stats
        pos += segment_len

    stats.decode_error = "jpeg_dimensions_not_found"
    return stats


def read_image_stats(path: Path) -> ImageStats:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return read_png_stats(path)
    if suffix in {".jpg", ".jpeg"}:
        return read_jpeg_dimensions(path)
    return ImageStats(decode_error="unsupported_image_format")


def add_reason(record: ImageRecord, points: int, reason: str) -> None:
    record.score += points
    record.reasons.append(reason)


def bbox_area(bbox: tuple[float, float, float, float] | None) -> float | None:
    if bbox is None:
        return None
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def score_record(
    record: ImageRecord,
    global_hash_counts: Counter[str],
    doc_hash_counts: Counter[tuple[str | None, str]],
    page_metadata: dict[tuple[str, int], PageMetadata],
) -> None:
    stats = record.stats
    metadata = record.metadata
    hard_drop = False

    if record.size_bytes <= 1_500:
        add_reason(record, 3, "tiny_file")
        hard_drop = True
    elif record.size_bytes <= 5_000:
        add_reason(record, 1, "small_file")

    if stats.width and stats.height:
        pixel_area = stats.width * stats.height
        pixel_aspect = max(stats.width / stats.height, stats.height / stats.width)
        compressed_bytes_per_pixel = record.size_bytes / max(1, pixel_area)

        if stats.width <= 48 or stats.height <= 48 or pixel_area <= 4_096:
            add_reason(record, 3, "tiny_pixel_asset")
            hard_drop = True
        elif (stats.width <= 120 and stats.height <= 120) or pixel_area <= 10_000:
            add_reason(record, 2, "small_pixel_asset")

        if pixel_aspect >= 12 and pixel_area <= 200_000:
            add_reason(record, 2, "thin_rule_or_strip")

        if compressed_bytes_per_pixel < 0.015 and pixel_area >= 20_000:
            add_reason(record, 2, "very_low_compressed_detail")
        elif compressed_bytes_per_pixel < 0.035 and pixel_area >= 20_000:
            add_reason(record, 1, "low_compressed_detail")

    bbox = metadata.bbox
    page_box = None
    page_text_chars = 0
    if metadata.document is not None and metadata.page_number is not None:
        page = page_metadata.get((metadata.document, metadata.page_number))
        if page:
            page_box = page.bbox
            page_text_chars = page.text_chars

    if bbox is not None:
        record.bbox_width = bbox[2] - bbox[0]
        record.bbox_height = bbox[3] - bbox[1]
        bbox_image_area = max(0.0, record.bbox_width * record.bbox_height)

        if record.bbox_width <= 24 or record.bbox_height <= 24 or bbox_image_area <= 400:
            add_reason(record, 3, "tiny_bbox")
            hard_drop = True
        elif record.bbox_width <= 80 or record.bbox_height <= 80 or bbox_image_area <= 6_400:
            add_reason(record, 2, "small_bbox")

        if record.bbox_width > 0 and record.bbox_height > 0:
            bbox_aspect = max(record.bbox_width / record.bbox_height, record.bbox_height / record.bbox_width)
            if bbox_aspect >= 15 and bbox_image_area <= 30_000:
                add_reason(record, 2, "thin_bbox_strip")

        if page_box is not None:
            page_w = max(1.0, page_box[2] - page_box[0])
            page_h = max(1.0, page_box[3] - page_box[1])
            page_area = page_w * page_h
            record.bbox_area_ratio = bbox_image_area / page_area
            near_bottom = bbox[1] <= page_box[1] + page_h * 0.12
            near_top = bbox[3] >= page_box[3] - page_h * 0.12
            narrow_band = record.bbox_height <= page_h * 0.10 or record.bbox_width <= page_w * 0.12

            if narrow_band and (near_bottom or near_top):
                add_reason(record, 1, "header_footer_or_margin")

            if record.bbox_area_ratio >= 0.85 and page_text_chars >= 120:
                add_reason(record, 3, "page_background_with_text_layer")
            elif record.bbox_area_ratio >= 0.60 and page_text_chars >= 300:
                add_reason(record, 2, "large_background_with_text_layer")

    if stats.transparent_ratio is not None and stats.transparent_ratio >= 0.90:
        add_reason(record, 3, "mostly_transparent")
        hard_drop = True

    if stats.white_or_transparent_ratio is not None and stats.white_or_transparent_ratio >= 0.98:
        add_reason(record, 2, "mostly_white_or_empty")

    if stats.dominant_color_ratio is not None and stats.dominant_color_ratio >= 0.98:
        add_reason(record, 3, "single_dominant_color")
    elif stats.dominant_color_ratio is not None and stats.dominant_color_ratio >= 0.94:
        add_reason(record, 1, "dominant_flat_color")

    if stats.unique_sample_colors is not None:
        if stats.unique_sample_colors <= 3:
            add_reason(record, 3, "few_sample_colors")
        elif stats.unique_sample_colors <= 8:
            add_reason(record, 1, "limited_sample_colors")

    if stats.luminance_stddev is not None:
        if stats.luminance_stddev < 4.0:
            add_reason(record, 3, "near_blank_luminance")
        elif stats.luminance_stddev < 8.0:
            add_reason(record, 1, "low_luminance_variation")

    global_repeats = global_hash_counts[record.sha256]
    doc_repeats = doc_hash_counts[(metadata.document, record.sha256)]
    if doc_repeats >= 3:
        add_reason(record, 2, f"repeated_exact_in_document:{doc_repeats}")
    elif global_repeats >= 4:
        add_reason(record, 1, f"repeated_exact_globally:{global_repeats}")

    if hard_drop or record.score >= 4:
        record.decision = "drop"
    elif record.score >= 2:
        record.decision = "review"
    else:
        record.decision = "candidate"


def manifest_row(record: ImageRecord) -> dict[str, Any]:
    stats = record.stats
    metadata = record.metadata
    return {
        "decision": record.decision,
        "decorative_score": record.score,
        "reasons": "|".join(record.reasons),
        "path": record.rel_path,
        "document": metadata.document or "",
        "page_number": metadata.page_number or "",
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "pixel_width": stats.width or "",
        "pixel_height": stats.height or "",
        "bbox_width": round(record.bbox_width, 3) if record.bbox_width is not None else "",
        "bbox_height": round(record.bbox_height, 3) if record.bbox_height is not None else "",
        "bbox_area_ratio": round(record.bbox_area_ratio, 6) if record.bbox_area_ratio is not None else "",
        "unique_sample_colors": stats.unique_sample_colors or "",
        "dominant_color_ratio": round(stats.dominant_color_ratio, 6)
        if stats.dominant_color_ratio is not None
        else "",
        "white_or_transparent_ratio": round(stats.white_or_transparent_ratio, 6)
        if stats.white_or_transparent_ratio is not None
        else "",
        "transparent_ratio": round(stats.transparent_ratio, 6)
        if stats.transparent_ratio is not None
        else "",
        "luminance_stddev": round(stats.luminance_stddev, 6)
        if stats.luminance_stddev is not None
        else "",
        "decode_error": stats.decode_error or "",
    }


def print_summary(records: list[ImageRecord], output_dir: Path, csv_path: Path) -> None:
    decision_counts = Counter(record.decision for record in records)
    reason_counts: Counter[str] = Counter()
    for record in records:
        for reason in record.reasons:
            reason_counts[reason.split(":", 1)[0]] += 1

    print("\nImage decorative pre-filter summary")
    print("-----------------------------------")
    print(f"Output folder: {output_dir}")
    print(f"Images analyzed: {len(records)}")
    for decision in ("candidate", "review", "drop"):
        print(f"{decision:>9}: {decision_counts[decision]}")

    print("\nTop reasons:")
    for reason, count in reason_counts.most_common(12):
        print(f"  {reason}: {count}")

    print("\nWrote:")
    print(f"  {csv_path}")


def build_prefilter_records(output_dir: Path) -> list[ImageRecord]:
    if not output_dir.exists():
        raise SystemExit(f"Output directory does not exist: {output_dir}")

    image_metadata, page_metadata = load_extraction_metadata(output_dir)
    image_paths = sorted(
        path
        for path in output_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() in IMAGE_EXTENSIONS
    )

    records: list[ImageRecord] = []
    for image_path in image_paths:
        rel_path = image_path.relative_to(output_dir).as_posix()
        metadata = image_metadata.get(rel_path, ImageMetadata(document=image_path.parent.name.removesuffix("_images")))
        records.append(
            ImageRecord(
                path=image_path,
                rel_path=rel_path,
                size_bytes=image_path.stat().st_size,
                sha256=sha256_file(image_path),
                metadata=metadata,
                stats=read_image_stats(image_path),
            )
        )

    global_hash_counts = Counter(record.sha256 for record in records)
    doc_hash_counts = Counter((record.metadata.document, record.sha256) for record in records)
    for record in records:
        score_record(record, global_hash_counts, doc_hash_counts, page_metadata)

    return records


def build_prefilter_rows(output_dir: Path) -> list[dict[str, Any]]:
    return [manifest_row(record) for record in build_prefilter_records(output_dir)]


def write_manifest(rows: list[dict[str, Any]], csv_path: Path) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()

    records = build_prefilter_records(output_dir)
    rows = [manifest_row(record) for record in records]
    csv_path = output_dir / f"{args.manifest_prefix}.csv"

    write_manifest(rows, csv_path)
    print_summary(records, output_dir, csv_path)


if __name__ == "__main__":
    main()
