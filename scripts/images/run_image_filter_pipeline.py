import argparse
from pathlib import Path

from filter_images_with_ocr import ensure_ocr_runtime_available, filter_images_from_rows
from prefilter_images import build_prefilter_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run image filtering in two steps: prefilter images from output, then "
            "OCR-filter the prefilter result into filtered_images."
        )
    )
    parser.add_argument("--output-dir", default="output", help="Folder with extracted PDF images.")
    parser.add_argument("--filtered-dir", default="filtered_images", help="Folder for final filtered images.")
    parser.add_argument("--lang", default="en", help="PaddleOCR language code.")
    parser.add_argument("--device", default="cpu", help="PaddleOCR device, for example cpu or gpu:0.")
    parser.add_argument("--ocr-version", default=None, help="Optional PaddleOCR model version.")
    parser.add_argument("--min-ocr-chars", type=int, default=25, help="Minimum alphanumeric OCR chars.")
    parser.add_argument("--min-ocr-lines", type=int, default=1, help="Minimum OCR text lines.")
    parser.add_argument("--min-confidence", type=float, default=0.45, help="Minimum average OCR confidence.")
    parser.add_argument(
        "--exclude-review",
        action="store_true",
        help="Only OCR images marked candidate by prefilter.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove filtered-dir before writing the final filtered images.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N prefiltered rows. Useful for OCR smoke tests.",
    )
    parser.add_argument(
        "--skip-ocr",
        action="store_true",
        help="Copy prefilter candidate/review images without OCR. Intended only for debugging.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    filtered_dir = Path(args.filtered_dir).resolve()

    if not output_dir.exists():
        raise SystemExit(f"Output directory does not exist: {output_dir}")
    if not args.skip_ocr:
        try:
            ensure_ocr_runtime_available()
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc

    print("Step 1/2: prefilter images")
    prefilter_rows = build_prefilter_rows(output_dir)
    if args.limit is not None:
        prefilter_rows = prefilter_rows[: args.limit]

    print("Step 2/2: filter prefiltered images with OCR")
    decision_counts = filter_images_from_rows(
        prefilter_rows,
        output_dir=output_dir,
        filtered_dir=filtered_dir,
        args=args,
        include_review=not args.exclude_review,
    )

    print("\nPipeline complete")
    print(f"Output folder: {output_dir}")
    print(f"Filtered images folder: {filtered_dir}")
    for status, count in sorted(decision_counts.items()):
        print(f"{status:>14}: {count}")


if __name__ == "__main__":
    main()
