import argparse
import csv
import importlib.util
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


@dataclass
class OcrSummary:
    text: str
    text_lines: list[str]
    text_char_count: int
    average_confidence: float | None
    max_confidence: float | None
    raw_line_count: int
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy cheap-prefiltered OCR-positive images into one filtered folder "
            "for RAG ingestion."
        )
    )
    parser.add_argument("--output-dir", default="output", help="Folder containing extracted images and prefilter manifest.")
    parser.add_argument("--filtered-dir", default="filtered_images", help="Destination folder for filtered images.")
    parser.add_argument(
        "--manifest",
        default=None,
        help="Path to image_prefilter_manifest.csv. Defaults to <output-dir>/image_prefilter_manifest.csv.",
    )
    parser.add_argument(
        "--include-review",
        action="store_true",
        default=True,
        help="Include prefilter 'review' images in OCR/copy stage. Enabled by default.",
    )
    parser.add_argument(
        "--exclude-review",
        action="store_true",
        help="Only process prefilter 'candidate' images.",
    )
    parser.add_argument("--lang", default="en", help="PaddleOCR language code.")
    parser.add_argument("--device", default="cpu", help="PaddleOCR device, for example cpu or gpu:0.")
    parser.add_argument("--ocr-version", default=None, help="Optional PaddleOCR model version, for example PP-OCRv4.")
    parser.add_argument("--min-ocr-chars", type=int, default=25, help="Minimum alphanumeric OCR chars for OCR-positive.")
    parser.add_argument("--min-ocr-lines", type=int, default=1, help="Minimum OCR text lines for OCR-positive.")
    parser.add_argument("--min-confidence", type=float, default=0.45, help="Minimum average OCR confidence for OCR-positive.")
    parser.add_argument(
        "--skip-ocr",
        action="store_true",
        help="Copy candidate/review images into filtered-dir without running OCR.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove the destination folder before writing a fresh filtered result.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N selected rows. Useful for OCR smoke tests.",
    )
    return parser.parse_args()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def useful_char_count(lines: list[str]) -> int:
    text = " ".join(line for line in lines if not is_logo_like_text(line))
    return len(re.findall(r"[A-Za-z0-9]", text))


def is_logo_like_text(text: str) -> bool:
    normalized = normalize_text(text).lower()
    normalized = normalized.strip(" .,:;|/\\-_")
    return normalized in {
        "",
        "ey",
        "ey.com",
        "ey global",
        "building a better working world",
        "©",
        "copyright",
    }


def safe_name(row: dict[str, str]) -> str:
    source = Path(row["path"])
    document = row.get("document") or source.parent.name.removesuffix("_images")
    page = row.get("page_number") or "NA"
    score = row.get("decorative_score") or "NA"
    raw_name = f"{document}__p{page}__{source.stem}__score{score}{source.suffix.lower()}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_name)


def load_manifest(manifest_path: Path, include_review: bool) -> list[dict[str, str]]:
    with manifest_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return select_manifest_rows(rows, include_review=include_review)


def to_builtin(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {key: to_builtin(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_builtin(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    for attr in ("json", "dict"):
        if hasattr(value, attr):
            attr_value = getattr(value, attr)
            if callable(attr_value):
                attr_value = attr_value()
            return to_builtin(attr_value)
    if hasattr(value, "__dict__"):
        return to_builtin(value.__dict__)
    return str(value)


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_text_pairs(value: Any) -> list[tuple[str, float | None]]:
    value = to_builtin(value)
    pairs: list[tuple[str, float | None]] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            if "rec_texts" in item:
                texts = to_builtin(item.get("rec_texts")) or []
                scores = to_builtin(item.get("rec_scores")) or []
                if not isinstance(texts, list):
                    texts = [texts]
                if not isinstance(scores, list):
                    scores = [scores]
                for index, text in enumerate(texts):
                    if isinstance(text, str):
                        score = as_float(scores[index]) if index < len(scores) else None
                        pairs.append((text, score))
            for child in item.values():
                walk(child)
            return

        if isinstance(item, list):
            if len(item) == 2 and isinstance(item[0], str):
                pairs.append((item[0], as_float(item[1])))
                return
            if len(item) == 2 and isinstance(item[1], list) and item[1] and isinstance(item[1][0], str):
                score = as_float(item[1][1]) if len(item[1]) > 1 else None
                pairs.append((item[1][0], score))
                return
            for child in item:
                walk(child)

    walk(value)

    deduped: list[tuple[str, float | None]] = []
    seen = set()
    for text, score in pairs:
        cleaned = normalize_text(text)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append((cleaned, score))
    return deduped


def ensure_ocr_runtime_available() -> None:
    if importlib.util.find_spec("paddle") is not None:
        return

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    raise RuntimeError(
        "PaddleOCR is installed, but its runtime dependency 'paddlepaddle' is missing. "
        f"This venv is using Python {python_version}; if pip reports no matching "
        "paddlepaddle distribution, create the venv with a PaddlePaddle-supported "
        "Python version such as 3.12, then install: python -m pip install paddlepaddle"
    )


def create_ocr(lang: str, device: str, ocr_version: str | None):
    ensure_ocr_runtime_available()

    workspace_dir = Path(__file__).resolve().parent
    paddle_cache_home = os.environ.get("PADDLE_PDX_CACHE_HOME")
    if paddle_cache_home:
        try:
            cache_path = Path(paddle_cache_home).resolve()
            cache_in_workspace = cache_path == workspace_dir or workspace_dir in cache_path.parents
        except OSError:
            cache_in_workspace = True
    else:
        cache_in_workspace = True
    if cache_in_workspace:
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(Path.home() / ".paddlex")

    os.environ.setdefault("FLAGS_enable_pir_api", "0")
    os.environ.setdefault("FLAGS_enable_pir_in_executor", "0")
    os.environ.setdefault("FLAGS_json_format_model", "0")
    os.environ.setdefault("FLAGS_use_mkldnn", "0")
    os.environ.setdefault("FLAGS_use_onednn", "0")
    os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MKLDNN_MODEL_BL", "1")

    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError(
            "PaddleOCR is not installed. Install it with: python -m pip install paddleocr paddlepaddle"
        ) from exc

    new_kwargs: dict[str, Any] = {
        "lang": lang,
        "device": device,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
    }
    if ocr_version:
        new_kwargs["ocr_version"] = ocr_version

    try:
        return PaddleOCR(**new_kwargs), "predict"
    except TypeError:
        old_kwargs: dict[str, Any] = {
            "lang": lang,
            "use_angle_cls": False,
            "show_log": False,
        }
        if ocr_version:
            old_kwargs["ocr_version"] = ocr_version
        return PaddleOCR(**old_kwargs), "ocr"


def run_ocr(ocr: Any, mode: str, image_path: Path) -> OcrSummary:
    try:
        if mode == "predict":
            raw_result = ocr.predict(str(image_path))
        else:
            raw_result = ocr.ocr(str(image_path), cls=False)
    except Exception as exc:
        return OcrSummary(
            text="",
            text_lines=[],
            text_char_count=0,
            average_confidence=None,
            max_confidence=None,
            raw_line_count=0,
            error=f"{type(exc).__name__}: {exc}",
        )

    pairs = extract_text_pairs(raw_result)
    lines = [text for text, _score in pairs if not is_logo_like_text(text)]
    scores = [score for _text, score in pairs if score is not None]
    average_confidence = sum(scores) / len(scores) if scores else None
    max_confidence = max(scores) if scores else None
    text = "\n".join(lines)
    return OcrSummary(
        text=text,
        text_lines=lines,
        text_char_count=useful_char_count(lines),
        average_confidence=average_confidence,
        max_confidence=max_confidence,
        raw_line_count=len(pairs),
    )


def is_ocr_positive(summary: OcrSummary, args: argparse.Namespace) -> bool:
    if summary.error:
        return False
    if summary.text_char_count < args.min_ocr_chars:
        return False
    if len(summary.text_lines) < args.min_ocr_lines:
        return False
    if summary.average_confidence is not None and summary.average_confidence < args.min_confidence:
        return False
    return True


def unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a free destination name for {path}")


def filter_images_from_rows(
    rows: list[dict[str, str]],
    output_dir: Path,
    filtered_dir: Path,
    args: argparse.Namespace,
    include_review: bool = True,
) -> dict[str, int]:
    selected_rows = select_manifest_rows(rows, include_review=include_review)
    ocr = None
    ocr_mode = None
    if not args.skip_ocr:
        ocr, ocr_mode = create_ocr(args.lang, args.device, args.ocr_version)

    if args.clean and filtered_dir.exists():
        shutil.rmtree(filtered_dir)
    filtered_dir.mkdir(parents=True, exist_ok=True)

    decision_counts = {"copied": 0, "missing_source": 0, "no_ocr": 0, "ocr_error": 0}
    ocr_errors: list[dict[str, str]] = []

    for index, row in enumerate(selected_rows, 1):
        source_path = output_dir / row["path"]
        if not source_path.exists():
            decision_counts["missing_source"] += 1
            continue

        if args.skip_ocr:
            should_copy = True
        else:
            print(f"[{index}/{len(selected_rows)}] OCR {row['path']}")
            summary = run_ocr(ocr, ocr_mode, source_path)
            if summary.error:
                decision_counts["ocr_error"] += 1
                ocr_errors.append(
                    {
                        "path": row["path"],
                        "document": row.get("document", ""),
                        "page_number": row.get("page_number", ""),
                        "error": summary.error,
                    }
                )
                should_copy = False
            else:
                should_copy = is_ocr_positive(summary, args)
                if not should_copy:
                    decision_counts["no_ocr"] += 1

        if not should_copy:
            continue

        destination = unique_destination(filtered_dir / safe_name(row))
        shutil.copy2(source_path, destination)
        decision_counts["copied"] += 1

    if ocr_errors:
        error_report_path = output_dir / "image_ocr_errors.csv"
        with error_report_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["path", "document", "page_number", "error"])
            writer.writeheader()
            writer.writerows(ocr_errors)

        print(f"\nOCR errors written to: {error_report_path}")
        print("First OCR errors:")
        seen_errors = set()
        printed = 0
        for error_row in ocr_errors:
            error = error_row["error"]
            if error in seen_errors:
                continue
            seen_errors.add(error)
            print(f"- {error}")
            printed += 1
            if printed >= 5:
                break

    return decision_counts


def select_manifest_rows(rows: list[dict[str, str]], include_review: bool) -> list[dict[str, str]]:
    allowed = {"candidate"}
    if include_review:
        allowed.add("review")

    selected = []
    for row in rows:
        if row.get("decision") not in allowed:
            continue
        source_path = row.get("path", "")
        if Path(source_path).suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        selected.append(row)
    return selected


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    filtered_dir = Path(args.filtered_dir).resolve()
    manifest_path = Path(args.manifest).resolve() if args.manifest else output_dir / "image_prefilter_manifest.csv"
    include_review = args.include_review and not args.exclude_review

    if not manifest_path.exists():
        raise SystemExit(f"Missing prefilter manifest: {manifest_path}")

    selected_rows = load_manifest(manifest_path, include_review=include_review)
    if args.limit is not None:
        selected_rows = selected_rows[: args.limit]
    decision_counts = filter_images_from_rows(
        selected_rows,
        output_dir=output_dir,
        filtered_dir=filtered_dir,
        args=args,
        include_review=include_review,
    )

    print("\nFiltered image summary")
    print("----------------------")
    print(f"Source manifest: {manifest_path}")
    print(f"Filtered folder: {filtered_dir}")
    print(f"Prefilter rows processed: {len(selected_rows)}")
    for status, count in sorted(decision_counts.items()):
        print(f"{status:>9}: {count}")


if __name__ == "__main__":
    main()
