#!/usr/bin/env python3
"""Run a single-document ingestion smoke test across available index builders.

This script is intentionally small and operational: it selects one source PDF,
indexes matching precomputed text chunks into temporary Qdrant collections in
parallel, and starts the PixelRAG visual-index builder for the same document.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]


@dataclass
class IngestionJob:
    name: str
    command: list[str]
    timeout_seconds: int


@dataclass
class IngestionResult:
    name: str
    command: list[str]
    returncode: int
    seconds: float
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def status(self) -> str:
        if self.timed_out:
            return "timeout"
        return "ok" if self.returncode == 0 else "failed"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test all ingestion paths in parallel with one source document."
    )
    parser.add_argument(
        "--doc",
        default=None,
        help=(
            "PDF filename, path, or bare stem to use. Defaults to the smallest "
            "PDF in data/ for a quick smoke test."
        ),
    )
    parser.add_argument("--data-dir", default="data", help="Folder containing source PDFs.")
    parser.add_argument("--chunks-dir", default="chunks", help="Folder containing chunk outputs.")
    parser.add_argument("--qdrant-url", default="http://localhost:6333", help="Qdrant REST URL.")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base URL.")
    parser.add_argument("--ollama-model", default="bge-m3", help="Ollama embedding model.")
    parser.add_argument("--max-workers", type=int, default=4, help="Text strategy workers.")
    parser.add_argument("--text-timeout", type=int, default=900, help="Seconds before stopping text ingestion.")
    parser.add_argument(
        "--pixelrag-timeout",
        type=int,
        default=1800,
        help="Seconds before stopping PixelRAG visual ingestion.",
    )
    parser.add_argument(
        "--limit-per-strategy",
        type=int,
        default=None,
        help="Optional cap on matching chunks per text strategy.",
    )
    parser.add_argument(
        "--collection-prefix",
        default="ey_rag_single_doc_smoke",
        help="Prefix for temporary Qdrant smoke-test collections.",
    )
    parser.add_argument(
        "--report",
        default="chunks/single_doc_parallel_ingestion_smoke_report.md",
        help="Markdown report path.",
    )
    parser.add_argument("--skip-text", action="store_true", help="Skip Qdrant/Ollama text ingestion.")
    parser.add_argument("--skip-pixelrag", action="store_true", help="Skip PixelRAG visual ingestion.")
    parser.add_argument(
        "--pixelrag-config-only",
        action="store_true",
        help="Only write PixelRAG config; do not run PixelRAG index build.",
    )
    parser.add_argument(
        "--no-recreate",
        action="store_true",
        help="Do not recreate temporary Qdrant collections before indexing.",
    )
    return parser.parse_args()


def safe_slug(value: str) -> str:
    chars = [char.lower() if char.isalnum() else "_" for char in value]
    slug = "_".join(part for part in "".join(chars).split("_") if part)
    return slug[:80] or "doc"


def resolve_doc(doc: str | None, data_dir: Path) -> Path:
    if doc is None:
        pdfs = sorted(data_dir.glob("*.pdf"), key=lambda path: (path.stat().st_size, path.name))
        if not pdfs:
            raise SystemExit(f"No PDFs found in {data_dir}.")
        return pdfs[0].resolve()

    raw = Path(doc)
    candidates = [raw]
    if not raw.is_absolute():
        candidates.append(data_dir / raw)
    if raw.suffix.lower() == ".md":
        candidates.append(data_dir / f"{raw.stem}.pdf")
    if not raw.suffix:
        candidates.append(data_dir / f"{raw.name}.pdf")

    for candidate in candidates:
        if candidate.exists():
            if candidate.suffix.lower() == ".md":
                pdf_candidate = data_dir / f"{candidate.stem}.pdf"
                if pdf_candidate.exists():
                    return pdf_candidate.resolve()
            return candidate.resolve()

    tried = ", ".join(str(candidate) for candidate in candidates)
    raise SystemExit(f"Could not resolve --doc. Tried: {tried}")


def command_text(command: list[str]) -> str:
    parts = []
    for item in command:
        if " " in item:
            parts.append(f'"{item}"')
        else:
            parts.append(item)
    return " ".join(parts)


def build_jobs(args: argparse.Namespace, doc_path: Path) -> list[IngestionJob]:
    jobs: list[IngestionJob] = []
    doc_stem = doc_path.stem
    doc_source_name = f"{doc_stem}.md"
    prefix = f"{safe_slug(args.collection_prefix)}_{safe_slug(doc_stem)}"

    if not args.skip_text:
        text_command = [
            sys.executable,
            str(WORKSPACE / "scripts" / "indexing" / "index_qdrant_ollama.py"),
            "--chunks-dir",
            str(WORKSPACE / args.chunks_dir),
            "--qdrant-url",
            args.qdrant_url,
            "--ollama-url",
            args.ollama_url,
            "--ollama-model",
            args.ollama_model,
            "--collection-prefix",
            prefix,
            "--source-name",
            doc_source_name,
            "--parallel-strategies",
            "--max-workers",
            str(args.max_workers),
            "--continue-on-error",
        ]
        if not args.no_recreate:
            text_command.append("--recreate")
        if args.limit_per_strategy is not None:
            text_command.extend(["--limit-per-strategy", str(args.limit_per_strategy)])
        jobs.append(IngestionJob("qdrant_text_parallel", text_command, args.text_timeout))

    if not args.skip_pixelrag:
        pixelrag_output = WORKSPACE / "pixelrag_indexes" / f"smoke_{safe_slug(doc_stem)}"
        pixelrag_command = [
            sys.executable,
            str(WORKSPACE / "scripts" / "indexing" / "build_pixelrag_visual_index.py"),
            "--source",
            str(doc_path),
            "--output",
            str(pixelrag_output),
            "--limit",
            "1",
        ]
        if not args.pixelrag_config_only:
            pixelrag_command.extend(["--build", "--force"])
        jobs.append(IngestionJob("pixelrag_visual", pixelrag_command, args.pixelrag_timeout))

    return jobs


def stop_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            text=True,
        )
    else:
        process.kill()


def run_job(job: IngestionJob) -> IngestionResult:
    start = time.time()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        job.command,
        cwd=WORKSPACE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=job.timeout_seconds)
        returncode = process.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_process_tree(process)
        stdout, stderr = process.communicate()
        returncode = 124
        stderr = (
            f"{stderr.rstrip()}\n\n"
            f"Timed out after {job.timeout_seconds} seconds and stopped the process tree."
        ).lstrip()

    return IngestionResult(
        name=job.name,
        command=job.command,
        returncode=returncode,
        seconds=round(time.time() - start, 2),
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
    )


def md_escape(value: str) -> str:
    return value.replace("|", "/")


def fenced_output(value: str) -> str:
    if not value.strip():
        return "_No output._"
    return f"```text\n{value.rstrip()}\n```"


def write_report(report_path: Path, doc_path: Path, results: list[IngestionResult]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# Single Document Parallel Ingestion Smoke Report",
        "",
        f"Generated: {generated}",
        f"Source document: `{doc_path}`",
        "",
        "| Ingestion | Status | Return code | Seconds | Command |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for result in results:
        lines.append(
            f"| {result.name} | {result.status} | {result.returncode} | "
            f"{result.seconds} | `{md_escape(command_text(result.command))}` |"
        )

    for result in results:
        lines.extend(
            [
                "",
                f"## {result.name}",
                "",
                f"Status: `{result.status}`",
                "",
                "### STDOUT",
                "",
                fenced_output(result.stdout),
                "",
                "### STDERR",
                "",
                fenced_output(result.stderr),
            ]
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    data_dir = (WORKSPACE / args.data_dir).resolve()
    doc_path = resolve_doc(args.doc, data_dir)
    jobs = build_jobs(args, doc_path)
    if not jobs:
        raise SystemExit("No ingestion jobs selected.")

    print(f"Source document: {doc_path}")
    print(f"Starting {len(jobs)} ingestion job(s) in parallel.")
    results: list[IngestionResult] = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        futures = {executor.submit(run_job, job): job.name for job in jobs}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"{result.name}: {result.status} in {result.seconds}s")

    results.sort(key=lambda result: result.name)
    report_path = (WORKSPACE / args.report).resolve()
    write_report(report_path, doc_path, results)
    print(f"Wrote report: {report_path}")

    if any(result.returncode != 0 for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
