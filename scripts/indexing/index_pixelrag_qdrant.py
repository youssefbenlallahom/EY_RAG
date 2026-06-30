#!/usr/bin/env python3
"""Run PixelRAG visual embedding and store the result in Qdrant.

This intentionally stops before PixelRAG's FAISS build step:

    source PDF/image -> PixelRAG tiles -> PixelRAG embeddings/*.npz -> Qdrant

Use this for a visual retrieval collection that lives beside the text
Qdrant/Ollama collections.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from qdrant_client import QdrantClient, models


POINT_NAMESPACE = uuid.UUID("6c6fb5db-0cfe-4506-974d-0938e17fd795")
SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
PAYLOAD_INDEXES = {
    "source_name": models.PayloadSchemaType.KEYWORD,
    "original_name": models.PayloadSchemaType.KEYWORD,
    "article_id": models.PayloadSchemaType.INTEGER,
    "tile_index": models.PayloadSchemaType.INTEGER,
    "chunk_index": models.PayloadSchemaType.INTEGER,
}


@dataclass
class StageResult:
    name: str
    status: str
    seconds: float
    note: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Index PixelRAG visual embeddings into Qdrant instead of FAISS."
    )
    parser.add_argument("--source", required=True, help="One PDF/image file or folder.")
    parser.add_argument(
        "--output",
        default="pixelrag_indexes/qdrant_smoke",
        help="PixelRAG working folder for staged source, tiles, and embedding shards.",
    )
    parser.add_argument("--qdrant-url", default="http://localhost:6333", help="Qdrant REST URL.")
    parser.add_argument("--collection", default="ey_rag_pixelrag_visual", help="Qdrant collection name.")
    parser.add_argument("--model", default="Qwen/Qwen3-VL-Embedding-2B", help="PixelRAG visual embedding model.")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda", "mps"], help="Embedding device.")
    parser.add_argument("--dpi", type=int, default=200, help="PDF rendering DPI.")
    parser.add_argument("--quality", type=int, default=85, help="Rendered JPEG quality.")
    parser.add_argument("--pages", default="1", help="Comma-separated PDF pages to render, or 'all'.")
    parser.add_argument("--embed-limit", type=int, default=1, help="Max visual chunks to embed for smoke tests.")
    parser.add_argument("--embed-timeout", type=int, default=900, help="Seconds before stopping PixelRAG embedding.")
    parser.add_argument("--upsert-batch-size", type=int, default=64, help="Qdrant upsert batch size.")
    parser.add_argument("--recreate", action="store_true", help="Recreate the Qdrant visual collection.")
    parser.add_argument("--force", action="store_true", help="Remove existing tiles/embeddings in output first.")
    parser.add_argument("--skip-render", action="store_true", help="Reuse existing rendered tiles.")
    parser.add_argument("--skip-embed", action="store_true", help="Reuse existing embedding shards.")
    parser.add_argument("--dry-run", action="store_true", help="Run local stages/checks without writing Qdrant points.")
    return parser.parse_args()


def workspace_runtime_bin() -> Path:
    return (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "bin"
    )


def subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    runtime_bin = workspace_runtime_bin()
    if runtime_bin.exists():
        env["PATH"] = f"{runtime_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def stop_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            text=True,
        )
    else:
        process.kill()


def run_command(command: list[str], timeout: int | None = None) -> StageResult:
    start = time.time()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=subprocess_env(),
        creationflags=creationflags,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        if stdout.strip():
            print(stdout.rstrip())
        if stderr.strip():
            print(stderr.rstrip(), file=sys.stderr)
        status = "ok" if process.returncode == 0 else "failed"
        return StageResult(" ".join(command), status, round(time.time() - start, 2), f"returncode={process.returncode}")
    except subprocess.TimeoutExpired:
        stop_process_tree(process)
        stdout, stderr = process.communicate()
        if stdout.strip():
            print(stdout.rstrip())
        if stderr.strip():
            print(stderr.rstrip(), file=sys.stderr)
        return StageResult(" ".join(command), "timeout", round(time.time() - start, 2), f"timeout={timeout}s")


def parse_pages(value: str) -> list[int] | None:
    if value.strip().lower() == "all":
        return None
    pages = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not pages:
        raise SystemExit("--pages must be 'all' or a comma-separated list of positive integers.")
    if any(page < 1 for page in pages):
        raise SystemExit("--pages values must be 1-based positive integers.")
    return pages


def source_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source] if source.suffix.lower() in SUPPORTED_EXTENSIONS else []
    if source.is_dir():
        return [
            path
            for path in sorted(source.rglob("*"))
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
    return []


def path_posix(path: Path) -> str:
    return path.resolve().as_posix()


def safe_name(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    return safe.strip("_") or "pixelrag_qdrant"


def stage_source(source: Path, output_dir: Path, limit: int = 1) -> list[dict[str, str]]:
    files = source_files(source)[:limit]
    if not files:
        raise SystemExit(f"No supported PixelRAG visual files found in {source}.")
    stage_dir = output_dir / "_source"
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    mapping: list[dict[str, str]] = []
    for index, file_path in enumerate(files):
        staged_name = f"{index:06d}{file_path.suffix.lower()}"
        staged_path = stage_dir / staged_name
        shutil.copy2(file_path, staged_path)
        mapping.append(
            {
                "pixelrag_id": f"{index:06d}",
                "staged_name": staged_name,
                "staged_path": path_posix(staged_path),
                "original_name": file_path.name,
                "original_path": path_posix(file_path),
            }
        )
    (output_dir / "_source_mapping.json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    return mapping


def clean_work_dirs(output_dir: Path) -> None:
    for name in ("tiles", "embeddings"):
        target = output_dir / name
        if target.exists():
            shutil.rmtree(target)


def render_sources(mapping: list[dict[str, str]], output_dir: Path, pages: list[int] | None, dpi: int, quality: int) -> int:
    from pixelrag_render.backends.pdf import render_pdf

    tiles_dir = output_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    rendered = 0
    articles: list[dict[str, str]] = []

    for item in mapping:
        staged = Path(item["staged_path"])
        articles.append({"title": item["original_name"], "url": item["original_path"]})
        if staged.suffix.lower() == ".pdf":
            render_pdf(staged, tiles_dir, pages=pages, dpi=dpi, quality=quality)
            rendered += 1
        else:
            tile_dir = tiles_dir / f"{staged.stem}.png.tiles"
            tile_dir.mkdir(parents=True, exist_ok=True)
            dest = tile_dir / staged.name
            shutil.copy2(staged, dest)
            (tile_dir / "tiles.json").write_text(
                json.dumps({"source": str(staged), "tiles": [staged.name], "complete": True}),
                encoding="utf-8",
            )
            rendered += 1

    (output_dir / "articles.json").write_text(json.dumps(articles, indent=2), encoding="utf-8")
    return rendered


def chunk_tiles(output_dir: Path) -> StageResult:
    return run_command(
        [
            sys.executable,
            "-m",
            "pixelrag_embed.chunk",
            "--shard-dir",
            str(output_dir / "tiles"),
            "--workers",
            "1",
        ]
    )


def embed_tiles(args: argparse.Namespace, output_dir: Path) -> StageResult:
    command = [
        sys.executable,
        "-m",
        "pixelrag_embed.embed_cpu",
        "--shard-dir",
        str(output_dir / "tiles"),
        "--output-dir",
        str(output_dir / "embeddings"),
        "--device",
        args.device,
        "--model",
        args.model,
    ]
    if args.embed_limit is not None:
        command.extend(["--limit", str(args.embed_limit)])
    return run_command(command, timeout=args.embed_timeout)


def shard_files(embeddings_dir: Path) -> list[Path]:
    return sorted(embeddings_dir.glob("shard_*.npz"))


def load_mapping(output_dir: Path) -> dict[int, dict[str, str]]:
    path = output_dir / "_source_mapping.json"
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {int(row["pixelrag_id"]): row for row in rows}


def tile_path(output_dir: Path, article_id: int, tile_index: int, chunk_index: int) -> str:
    article_dir = output_dir / "tiles" / f"{article_id:06d}.png.tiles"
    chunks_json = article_dir / "chunks.json"
    if chunks_json.exists():
        manifest = json.loads(chunks_json.read_text(encoding="utf-8"))
        for chunk in manifest.get("chunks", []):
            if int(chunk.get("tile_index", 0)) == tile_index and int(chunk.get("chunk_index", 0)) == chunk_index:
                return path_posix(article_dir / str(chunk.get("file")))
    tiles_json = article_dir / "tiles.json"
    if tiles_json.exists():
        manifest = json.loads(tiles_json.read_text(encoding="utf-8"))
        tiles = manifest.get("tiles", [])
        if 0 <= tile_index < len(tiles):
            return path_posix(article_dir / str(tiles[tile_index]))
    return ""


def point_id(collection: str, article_id: int, tile_index: int, chunk_index: int) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, f"{collection}:{article_id}:{tile_index}:{chunk_index}"))


def iter_points(output_dir: Path, collection: str) -> Iterable[models.PointStruct]:
    mapping = load_mapping(output_dir)
    for shard_path in shard_files(output_dir / "embeddings"):
        with np.load(shard_path) as data:
            embeddings = data["embeddings"].astype(np.float32)
            article_ids = data["article_ids"]
            tile_indices = data["tile_indices"]
            chunk_indices = data["chunk_indices"]
            y_offsets = data["y_offsets"]
            tile_heights = data["tile_heights"]
            for row in range(embeddings.shape[0]):
                article_id = int(article_ids[row])
                tile_index = int(tile_indices[row])
                chunk_index = int(chunk_indices[row])
                source = mapping.get(article_id, {})
                payload: dict[str, Any] = {
                    "source_name": source.get("original_name", ""),
                    "original_name": source.get("original_name", ""),
                    "original_path": source.get("original_path", ""),
                    "staged_path": source.get("staged_path", ""),
                    "article_id": article_id,
                    "tile_index": tile_index,
                    "chunk_index": chunk_index,
                    "y_offset": int(y_offsets[row]),
                    "tile_height": int(tile_heights[row]),
                    "tile_path": tile_path(output_dir, article_id, tile_index, chunk_index),
                    "pixelrag_output": path_posix(output_dir),
                }
                yield models.PointStruct(
                    id=point_id(collection, article_id, tile_index, chunk_index),
                    vector=embeddings[row].tolist(),
                    payload=payload,
                )


def ensure_collection(client: QdrantClient, collection: str, vector_size: int, recreate: bool) -> None:
    if client.collection_exists(collection):
        if recreate:
            client.delete_collection(collection, timeout=120)
        else:
            return
    client.create_collection(
        collection_name=collection,
        vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
        on_disk_payload=True,
        timeout=120,
    )
    for field, schema in PAYLOAD_INDEXES.items():
        try:
            client.create_payload_index(
                collection_name=collection,
                field_name=field,
                field_schema=schema,
                wait=True,
                timeout=120,
            )
        except Exception as exc:
            print(f"Warning: could not create payload index {collection}.{field}: {exc}")


def first_vector_size(output_dir: Path) -> int:
    shards = shard_files(output_dir / "embeddings")
    if not shards:
        raise SystemExit(f"No PixelRAG embedding shards found under {output_dir / 'embeddings'}.")
    with np.load(shards[0]) as data:
        embeddings = data["embeddings"]
        if embeddings.ndim != 2 or embeddings.shape[0] == 0:
            raise SystemExit(f"Embedding shard has no vectors: {shards[0]}")
        return int(embeddings.shape[1])


def batched(items: Iterable[models.PointStruct], size: int) -> Iterable[list[models.PointStruct]]:
    batch: list[models.PointStruct] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def index_qdrant(args: argparse.Namespace, output_dir: Path) -> int:
    vector_size = first_vector_size(output_dir)
    client = QdrantClient(url=args.qdrant_url, timeout=120)
    ensure_collection(client, args.collection, vector_size, args.recreate)
    indexed = 0
    if args.dry_run:
        return 0
    for batch in batched(iter_points(output_dir, args.collection), args.upsert_batch_size):
        client.upsert(collection_name=args.collection, points=batch, wait=True, timeout=120)
        indexed += len(batch)
    return indexed


def write_report(output_dir: Path, args: argparse.Namespace, stages: list[StageResult], indexed: int) -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# PixelRAG to Qdrant Smoke Report",
        "",
        f"Generated: {generated}",
        f"Source: `{args.source}`",
        f"Output: `{output_dir}`",
        f"Qdrant collection: `{args.collection}`",
        "",
        "| Stage | Status | Seconds | Note |",
        "| --- | --- | ---: | --- |",
    ]
    for stage in stages:
        lines.append(f"| {stage.name} | {stage.status} | {stage.seconds} | {stage.note} |")
    lines.extend(["", f"Indexed points: `{indexed}`", ""])
    report_path = output_dir / f"qdrant_{safe_name(args.collection)}_report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    source = Path(args.source).resolve()
    if not source.exists():
        raise SystemExit(f"Source path does not exist: {source}")
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.force:
        clean_work_dirs(output_dir)

    stages: list[StageResult] = []
    mapping = stage_source(source, output_dir, limit=1)

    if not args.skip_render:
        start = time.time()
        rendered = render_sources(mapping, output_dir, parse_pages(args.pages), args.dpi, args.quality)
        stages.append(StageResult("render", "ok", round(time.time() - start, 2), f"documents={rendered}, pages={args.pages}"))
    else:
        stages.append(StageResult("render", "skipped", 0, "reused existing tiles"))

    chunk_result = chunk_tiles(output_dir)
    stages.append(StageResult("chunk", chunk_result.status, chunk_result.seconds, chunk_result.note))
    if chunk_result.status != "ok":
        write_report(output_dir, args, stages, 0)
        raise SystemExit(1)

    if not args.skip_embed:
        embed_result = embed_tiles(args, output_dir)
        stages.append(StageResult("embed", embed_result.status, embed_result.seconds, embed_result.note))
        if embed_result.status != "ok":
            write_report(output_dir, args, stages, 0)
            raise SystemExit(1)
    else:
        stages.append(StageResult("embed", "skipped", 0, "reused existing shards"))

    indexed = index_qdrant(args, output_dir)
    stages.append(StageResult("qdrant_upsert", "ok", 0, f"points={indexed}"))
    write_report(output_dir, args, stages, indexed)
    print(f"Indexed {indexed} PixelRAG visual point(s) into Qdrant collection {args.collection}.")
    print(f"Report: {output_dir / f'qdrant_{safe_name(args.collection)}_report.md'}")


if __name__ == "__main__":
    main()
