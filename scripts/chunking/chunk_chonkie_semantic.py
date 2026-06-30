#!/usr/bin/env python3
"""Chunk cleaned Markdown with Chonkie SemanticChunker.

Default input:  output_clean/
Default output: chunks/chonkie_semantic/chunks.jsonl

Install dependency:
    pip install "chonkie[semantic]"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ey_rag.rag_chunk_utils import (
    clean_chunk_text,
    heading_path_for_text,
    is_structured_block,
    make_record,
    parse_csv_arg,
    source_files,
    split_pages,
    write_outputs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create RAG chunks using Chonkie's SemanticChunker. "
            "This is useful as a Qdrant-friendly local chunking option before ingestion."
        )
    )
    parser.add_argument("--input-dir", default="output_clean", help="Clean Markdown folder.")
    parser.add_argument(
        "--output-dir",
        default="chunks/chonkie_semantic",
        help="Folder for chunks.jsonl and reports.",
    )
    parser.add_argument(
        "--embedding-model",
        default="minishlab/potion-base-32M",
        help="Embedding model used internally by Chonkie SemanticChunker.",
    )
    parser.add_argument(
        "--chunk-tokens",
        type=int,
        default=800,
        help="Approximate maximum chunk size passed to Chonkie when supported.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        help="Semantic similarity threshold passed to Chonkie when supported.",
    )
    parser.add_argument(
        "--similarity-window",
        type=int,
        default=3,
        help="Neighboring sentence window passed to Chonkie when supported.",
    )
    parser.add_argument(
        "--skip-window",
        type=int,
        default=0,
        help="Sentence skip window passed to Chonkie when supported.",
    )
    parser.add_argument(
        "--overlap-context",
        type=int,
        default=0,
        help="Optional Chonkie OverlapRefinery context size. Keep 0 for no refinement.",
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


def require_chonkie():
    try:
        from chonkie import SemanticChunker  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: chonkie\n"
            "Install it with:\n"
            '  venv\\Scripts\\python.exe -m pip install "chonkie[semantic]"'
        ) from exc
    return SemanticChunker


def build_chunker(SemanticChunker: Any, args: argparse.Namespace):
    full_kwargs = {
        "embedding_model": args.embedding_model,
        "chunk_size": args.chunk_tokens,
        "threshold": args.threshold,
        "similarity_window": args.similarity_window,
        "skip_window": args.skip_window,
    }
    attempts = [
        full_kwargs,
        {key: value for key, value in full_kwargs.items() if key != "skip_window"},
        {
            key: value
            for key, value in full_kwargs.items()
            if key not in {"skip_window", "similarity_window"}
        },
        {"embedding_model": args.embedding_model},
        {},
    ]
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            return SemanticChunker(**kwargs), kwargs
        except TypeError as exc:
            last_error = exc
    raise SystemExit(f"Could not initialize Chonkie SemanticChunker: {last_error}")


def chunk_with_chonkie(chunker: Any, text: str) -> list[Any]:
    if callable(chunker):
        try:
            result = chunker(text)
            return list(result)
        except TypeError:
            pass
    if hasattr(chunker, "chunk"):
        return list(chunker.chunk(text))
    raise SystemExit("Chonkie chunker has neither __call__ nor .chunk(text).")


def apply_optional_overlap(chunks: list[Any], context_size: int) -> list[Any]:
    if context_size <= 0 or not chunks:
        return chunks
    try:
        from chonkie import OverlapRefinery  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing Chonkie OverlapRefinery support.\n"
            'Install/upgrade with:\n'
            '  venv\\Scripts\\python.exe -m pip install "chonkie[semantic]"'
        ) from exc

    refinery = OverlapRefinery(context_size=context_size)
    if callable(refinery):
        return list(refinery(chunks))
    if hasattr(refinery, "refine"):
        return list(refinery.refine(chunks))
    raise SystemExit("Chonkie OverlapRefinery has neither __call__ nor .refine(chunks).")


def object_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        try:
            data = value.to_dict()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    result: dict[str, Any] = {}
    for key in ("text", "content", "start_index", "end_index", "token_count", "sentence_count"):
        if hasattr(value, key):
            result[key] = getattr(value, key)
    return result


def chunk_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "page_content"):
            if value.get(key):
                return str(value[key])
    for key in ("text", "content", "page_content"):
        if hasattr(value, key):
            data = getattr(value, key)
            if data:
                return str(data)
    return str(value)


def main() -> None:
    args = parse_args()
    SemanticChunker = require_chonkie()
    chonkie_chunker, used_kwargs = build_chunker(SemanticChunker, args)

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    include_types = parse_csv_arg(args.include_page_types)
    exclude_types = parse_csv_arg(args.exclude_page_types)

    chunks: list[dict[str, object]] = []
    sequence = 0
    files = source_files(input_dir, args.limit)

    for path in files:
        pages = split_pages(path, include_types, exclude_types)
        for page in pages:
            if args.protect_structured_blocks and is_structured_block(page.text, page.page_type):
                sequence += 1
                headings = heading_path_for_text(page.text, page.inherited_headings)
                chunks.append(
                    make_record(
                        "chonkie_semantic",
                        path,
                        sequence,
                        page.text,
                        page.number,
                        page.number,
                        page.page_type,
                        headings,
                        {
                            "structured_protected": True,
                            "chonkie_kwargs": json.dumps(used_kwargs, sort_keys=True),
                        },
                    )
                )
                continue

            raw_chunks = chunk_with_chonkie(chonkie_chunker, page.text)
            raw_chunks = apply_optional_overlap(raw_chunks, args.overlap_context)
            for raw_chunk in raw_chunks:
                text = clean_chunk_text(chunk_text(raw_chunk))
                if not text:
                    continue
                sequence += 1
                headings = heading_path_for_text(text, page.inherited_headings)
                metadata = object_to_dict(raw_chunk)
                metadata.pop("text", None)
                metadata.pop("content", None)
                metadata["chonkie_kwargs"] = json.dumps(used_kwargs, sort_keys=True)
                if args.overlap_context > 0:
                    metadata["overlap_context"] = args.overlap_context
                chunks.append(
                    make_record(
                        "chonkie_semantic",
                        path,
                        sequence,
                        text,
                        page.number,
                        page.number,
                        page.page_type,
                        headings,
                        metadata,
                    )
                )

    write_outputs(output_dir, chunks, len(files), "Chonkie Semantic Chunking Report")
    print(f"Wrote {len(chunks)} chunks to {output_dir / 'chunks.jsonl'}")


if __name__ == "__main__":
    main()
