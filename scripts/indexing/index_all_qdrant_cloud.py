#!/usr/bin/env python3
"""Run every EY RAG ingestion method against Qdrant Cloud.

This is an orchestrator. It reuses the project-specific indexers instead of
duplicating ingestion logic:

1. Text RAG collections from chunk JSONL files via Ollama bge-m3 embeddings.
2. PixelRAG visual embeddings into a separate Qdrant visual collection.

Cloud credentials are read from `.env`:

    QDRANT_CLOUD_URL=...
    QDRANT_CLOUD_API_KEY=...
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


TEXT_STRATEGIES = (
    "langchain_markdown_recursive",
    "haystack_document_splitter",
    "llamaindex_semantic",
    "chonkie_semantic",
)


@dataclass
class RunResult:
    name: str
    status: str
    seconds: float
    command: str


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def env_value(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Run all EY RAG ingestion methods into Qdrant Cloud."
    )
    parser.add_argument("--chunks-dir", default="chunks", help="Chunk JSONL root folder.")
    parser.add_argument("--collection-prefix", default="ey_rag", help="Text collection prefix.")
    parser.add_argument(
        "--text-strategies",
        default=",".join(TEXT_STRATEGIES),
        help="Comma-separated text strategies, or 'all'.",
    )
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Local Ollama URL.")
    parser.add_argument("--ollama-model", default="bge-m3", help="Local Ollama embedding model.")
    parser.add_argument("--text-embed-batch-size", type=int, default=8)
    parser.add_argument("--text-upsert-batch-size", type=int, default=32)
    parser.add_argument("--text-limit-per-strategy", type=int, default=None)
    parser.add_argument("--parallel-text", action="store_true", help="Index text strategies in parallel.")
    parser.add_argument("--text-workers", type=int, default=4)
    parser.add_argument("--skip-text", action="store_true", help="Skip the four text chunking methods.")

    parser.add_argument("--pixelrag-source", default="data", help="PixelRAG source file/folder.")
    parser.add_argument(
        "--pixelrag-output",
        default="pixelrag_indexes/ey_visual_index_cloud",
        help="PixelRAG working/output folder.",
    )
    parser.add_argument("--pixelrag-collection", default="ey_rag_pixelrag_visual")
    parser.add_argument("--pixelrag-pages", default="all")
    parser.add_argument("--pixelrag-source-limit", type=int, default=0)
    parser.add_argument("--pixelrag-embed-limit", type=int, default=0)
    parser.add_argument("--pixelrag-device", default="cuda", choices=["auto", "cpu", "cuda", "mps"])
    parser.add_argument("--pixelrag-embed-timeout", type=int, default=86400)
    parser.add_argument("--pixelrag-heartbeat-seconds", type=int, default=120)
    parser.add_argument("--pixelrag-upsert-batch-size", type=int, default=16)
    parser.add_argument("--pixelrag-render-retries", type=int, default=3)
    parser.add_argument("--pixelrag-render-fallback-dpi", type=int, default=150)
    parser.add_argument("--pixelrag-fail-on-render-error", action="store_true")
    parser.add_argument(
        "--pixelrag-force",
        action="store_true",
        help="Delete existing PixelRAG tiles/embeddings before rebuilding them.",
    )
    parser.add_argument("--skip-pixelrag", action="store_true", help="Skip PixelRAG visual ingestion.")
    parser.add_argument("--pixelrag-skip-render", action="store_true", help="Reuse existing PixelRAG tiles.")
    parser.add_argument("--pixelrag-skip-embed", action="store_true", help="Reuse existing PixelRAG embedding shards.")

    parser.add_argument("--recreate", action="store_true", help="Recreate target collections.")
    parser.add_argument("--no-resume", action="store_true", help="Disable resume/skip of already indexed points.")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--qdrant-retries", type=int, default=12)
    parser.add_argument("--qdrant-backoff", type=float, default=2.0)
    parser.add_argument("--qdrant-max-sleep", type=float, default=120.0)
    parser.add_argument("--qdrant-jitter", type=float, default=1.0)
    args = parser.parse_args()

    if not env_value("QDRANT_CLOUD_URL"):
        raise SystemExit("QDRANT_CLOUD_URL is missing from .env.")
    if not env_value("QDRANT_CLOUD_API_KEY"):
        raise SystemExit("QDRANT_CLOUD_API_KEY is missing from .env.")
    return args


def command_text(args: argparse.Namespace) -> list[str]:
    strategies = "all" if args.text_strategies.strip().lower() == "all" else args.text_strategies
    command = [
        sys.executable,
        "scripts/indexing/index_qdrant_ollama.py",
        "--cloud",
        "--chunks-dir",
        args.chunks_dir,
        "--collection-prefix",
        args.collection_prefix,
        "--strategies",
        strategies,
        "--ollama-url",
        args.ollama_url,
        "--ollama-model",
        args.ollama_model,
        "--embed-batch-size",
        str(args.text_embed_batch_size),
        "--upsert-batch-size",
        str(args.text_upsert_batch_size),
        "--timeout",
        str(args.timeout),
        "--qdrant-retries",
        str(args.qdrant_retries),
        "--qdrant-backoff",
        str(args.qdrant_backoff),
        "--qdrant-max-sleep",
        str(args.qdrant_max_sleep),
        "--qdrant-jitter",
        str(args.qdrant_jitter),
    ]
    if args.continue_on_error:
        command.append("--continue-on-error")
    if args.text_limit_per_strategy is not None:
        command.extend(["--limit-per-strategy", str(args.text_limit_per_strategy)])
    if args.parallel_text:
        command.extend(["--parallel-strategies", "--max-workers", str(args.text_workers)])
    if args.recreate:
        command.append("--recreate")
    if args.dry_run:
        command.append("--dry-run")
    if args.no_resume:
        command.append("--no-resume")
    return command


def command_pixelrag(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "scripts/indexing/index_pixelrag_qdrant.py",
        "--cloud",
        "--source",
        args.pixelrag_source,
        "--output",
        args.pixelrag_output,
        "--collection",
        args.pixelrag_collection,
        "--pages",
        args.pixelrag_pages,
        "--source-limit",
        str(args.pixelrag_source_limit),
        "--embed-limit",
        str(args.pixelrag_embed_limit),
        "--device",
        args.pixelrag_device,
        "--embed-timeout",
        str(args.pixelrag_embed_timeout),
        "--heartbeat-seconds",
        str(args.pixelrag_heartbeat_seconds),
        "--upsert-batch-size",
        str(args.pixelrag_upsert_batch_size),
        "--render-retries",
        str(args.pixelrag_render_retries),
        "--render-fallback-dpi",
        str(args.pixelrag_render_fallback_dpi),
        "--qdrant-timeout",
        str(args.timeout),
        "--qdrant-retries",
        str(args.qdrant_retries),
        "--qdrant-backoff",
        str(args.qdrant_backoff),
        "--qdrant-max-sleep",
        str(args.qdrant_max_sleep),
        "--qdrant-jitter",
        str(args.qdrant_jitter),
    ]
    if args.recreate:
        command.append("--recreate")
    if args.dry_run:
        command.append("--dry-run")
    if args.no_resume:
        command.append("--no-resume")
    if args.pixelrag_fail_on_render_error:
        command.append("--fail-on-render-error")
    if args.pixelrag_skip_render:
        command.append("--skip-render")
    if args.pixelrag_skip_embed:
        command.append("--skip-embed")
    if args.pixelrag_force:
        command.append("--force")
    return command


def printable(command: list[str]) -> str:
    return " ".join(command)


def run_stage(name: str, command: list[str], continue_on_error: bool) -> RunResult:
    print("")
    print(f"=== {name} ===", flush=True)
    print(printable(command), flush=True)
    started = time.time()
    try:
        subprocess.run(command, check=True)
        return RunResult(name, "ok", round(time.time() - started, 2), printable(command))
    except subprocess.CalledProcessError as exc:
        result = RunResult(
            name,
            f"failed_returncode_{exc.returncode}",
            round(time.time() - started, 2),
            printable(command),
        )
        if continue_on_error:
            print(f"{name} failed but --continue-on-error is enabled.", flush=True)
            return result
        raise


def write_report(results: list[RunResult]) -> None:
    report = Path("scripts/indexing/qdrant_cloud_ingestion_report.md")
    lines = [
        "# Qdrant Cloud Ingestion Report",
        "",
        "| Stage | Status | Seconds | Command |",
        "| --- | --- | ---: | --- |",
    ]
    for result in results:
        lines.append(
            f"| {result.name} | {result.status} | {result.seconds} | `{result.command}` |"
        )
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote report: {report}", flush=True)


def main() -> None:
    args = parse_args()
    print("Qdrant Cloud URL loaded from .env.")
    print("API key loaded from .env; it will not be printed.")

    results: list[RunResult] = []
    if not args.skip_text:
        results.append(run_stage("text_chunking_methods", command_text(args), args.continue_on_error))
    if not args.skip_pixelrag:
        results.append(run_stage("pixelrag_visual_method", command_pixelrag(args), args.continue_on_error))
    if args.skip_text and args.skip_pixelrag:
        raise SystemExit("Nothing to run: both --skip-text and --skip-pixelrag were selected.")
    write_report(results)


if __name__ == "__main__":
    main()
