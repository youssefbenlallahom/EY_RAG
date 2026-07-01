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
import random
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
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Index PixelRAG visual embeddings into Qdrant instead of FAISS."
    )
    parser.add_argument("--source", default="data", help="One PDF/image file or folder.")
    parser.add_argument(
        "--output",
        default="pixelrag_indexes/qdrant_smoke",
        help="PixelRAG working folder for staged source, tiles, and embedding shards.",
    )
    parser.add_argument("--qdrant-url", default=env_value("QDRANT_URL", default="http://localhost:6333"), help="Qdrant REST URL.")
    parser.add_argument("--qdrant-api-key", default=env_value("QDRANT_API_KEY"), help="Optional Qdrant API key.")
    parser.add_argument("--cloud", action="store_true", help="Use QDRANT_CLOUD_URL and QDRANT_CLOUD_API_KEY from .env.")
    parser.add_argument("--collection", default="ey_rag_pixelrag_visual", help="Qdrant collection name.")
    parser.add_argument("--model", default="Qwen/Qwen3-VL-Embedding-2B", help="PixelRAG visual embedding model.")
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda", "mps"], help="Embedding device.")
    parser.add_argument("--dpi", type=int, default=200, help="PDF rendering DPI.")
    parser.add_argument("--quality", type=int, default=85, help="Rendered JPEG quality.")
    parser.add_argument("--render-retries", type=int, default=3, help="Retries per PDF page during rendering.")
    parser.add_argument("--render-fallback-dpi", type=int, default=150, help="Lower DPI retry for difficult PDF pages.")
    parser.add_argument("--fail-on-render-error", action="store_true", help="Abort if a PDF page cannot be rendered.")
    parser.add_argument("--pages", default="1", help="Comma-separated PDF pages to render, or 'all'.")
    parser.add_argument(
        "--source-limit",
        type=int,
        default=0,
        help="Max source documents to stage. Use 0 for all documents.",
    )
    parser.add_argument(
        "--embed-limit",
        type=int,
        default=0,
        help="Max visual chunks to embed for smoke tests. Use 0 for all chunks.",
    )
    parser.add_argument("--embed-timeout", type=int, default=900, help="Seconds before stopping PixelRAG embedding.")
    parser.add_argument("--upsert-batch-size", type=int, default=64, help="Qdrant upsert batch size.")
    parser.add_argument("--recreate", action="store_true", help="Recreate the Qdrant visual collection.")
    parser.add_argument("--force", action="store_true", help="Remove existing tiles/embeddings in output first.")
    parser.add_argument("--skip-render", action="store_true", help="Reuse existing rendered tiles.")
    parser.add_argument("--skip-embed", action="store_true", help="Reuse existing embedding shards.")
    parser.add_argument("--dry-run", action="store_true", help="Run local stages/checks without writing Qdrant points.")
    parser.add_argument("--check-cuda", action="store_true", help="Print CUDA/GPU diagnostics and exit.")
    parser.add_argument(
        "--heartbeat-seconds",
        type=int,
        default=60,
        help="Print a heartbeat while long subprocess stages are still running.",
    )
    parser.add_argument("--qdrant-timeout", type=int, default=300, help="Qdrant operation timeout seconds.")
    parser.add_argument("--qdrant-retries", type=int, default=10, help="Retries for transient Qdrant HTTP/SSL errors.")
    parser.add_argument("--qdrant-backoff", type=float, default=2.0, help="Initial Qdrant retry backoff seconds.")
    parser.add_argument("--qdrant-max-sleep", type=float, default=120.0, help="Maximum Qdrant retry sleep seconds.")
    parser.add_argument("--qdrant-jitter", type=float, default=1.0, help="Random jitter added to Qdrant retry sleeps.")
    parser.add_argument("--no-resume", action="store_true", help="Do not skip already indexed visual points when rerunning.")
    args = parser.parse_args()
    if args.cloud:
        args.qdrant_url = env_value("QDRANT_CLOUD_URL")
        args.qdrant_api_key = env_value("QDRANT_CLOUD_API_KEY")
        if not args.qdrant_url:
            raise SystemExit("QDRANT_CLOUD_URL is missing from .env.")
        if not args.qdrant_api_key:
            raise SystemExit("QDRANT_CLOUD_API_KEY is missing from .env.")
    return args


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


def retry_call(
    label: str,
    retries: int,
    backoff: float,
    max_sleep: float,
    jitter: float,
    func: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    attempts = max(retries, 0) + 1
    for attempt in range(1, attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if not should_retry_exception(exc):
                print(
                    f"{label}: non-retryable error: {type(exc).__name__}: {str(exc)[:500]}",
                    flush=True,
                )
                raise
            if attempt >= attempts:
                print(
                    f"{label}: failed after {attempt}/{attempts} attempts: "
                    f"{type(exc).__name__}: {str(exc)[:500]}",
                    flush=True,
                )
                raise
            sleep_seconds = min(max_sleep, backoff * (2 ** (attempt - 1)))
            if jitter > 0:
                sleep_seconds += random.uniform(0, jitter)
            print(
                f"{label}: transient failure {attempt}/{attempts}: "
                f"{type(exc).__name__}: {str(exc)[:300]}. "
                f"Retrying in {sleep_seconds:.1f}s ...",
                flush=True,
            )
            time.sleep(sleep_seconds)
    raise RuntimeError(f"Unreachable retry path for {label}")


def should_retry_exception(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    non_retryable = (
        "400",
        "401",
        "403",
        "404",
        "409",
        "bad request",
        "unauthorized",
        "forbidden",
        "not found",
        "already exists",
        "wrong input",
        "validation",
    )
    return not any(marker in text for marker in non_retryable)


def qdrant_call(args: argparse.Namespace, label: str, func: Any, *func_args: Any, **kwargs: Any) -> Any:
    return retry_call(
        label,
        args.qdrant_retries,
        args.qdrant_backoff,
        args.qdrant_max_sleep,
        args.qdrant_jitter,
        func,
        *func_args,
        **kwargs,
    )


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


def run_command(command: list[str], timeout: int | None = None, heartbeat_seconds: int = 60) -> StageResult:
    start = time.time()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    print("Running:", " ".join(command), flush=True)
    process = subprocess.Popen(
        command,
        env=subprocess_env(),
        creationflags=creationflags,
    )
    try:
        next_heartbeat = time.time() + max(heartbeat_seconds, 0)
        while True:
            elapsed = time.time() - start
            if timeout is not None and elapsed >= timeout:
                stop_process_tree(process)
                return StageResult(" ".join(command), "timeout", round(elapsed, 2), f"timeout={timeout}s")
            wait_seconds = 5
            if timeout is not None:
                wait_seconds = max(0.1, min(wait_seconds, timeout - elapsed))
            try:
                process.wait(timeout=wait_seconds)
                break
            except subprocess.TimeoutExpired:
                if heartbeat_seconds > 0 and time.time() >= next_heartbeat:
                    print(
                        f"Still running: {' '.join(command)} "
                        f"(elapsed {round(time.time() - start)}s)",
                        flush=True,
                    )
                    next_heartbeat += heartbeat_seconds
        status = "ok" if process.returncode == 0 else "failed"
        return StageResult(" ".join(command), status, round(time.time() - start, 2), f"returncode={process.returncode}")
    except KeyboardInterrupt:
        stop_process_tree(process)
        raise


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


def stage_source(source: Path, output_dir: Path, limit: int = 0) -> list[dict[str, str]]:
    files = source_files(source)
    if limit > 0:
        files = files[:limit]
    if not files:
        raise SystemExit(f"No supported PixelRAG visual files found in {source}.")
    stage_dir = output_dir / "_source"
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    mapping: list[dict[str, str]] = []
    print(f"Staging {len(files)} source file(s) into {stage_dir} ...", flush=True)
    for index, file_path in enumerate(files):
        print(f"  [{index + 1}/{len(files)}] {file_path.name}", flush=True)
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


def existing_manifest_complete(tile_dir: Path, expected_tiles: list[str]) -> bool:
    tiles_json = tile_dir / "tiles.json"
    chunks_json = tile_dir / "chunks.json"
    if not tiles_json.exists() or not chunks_json.exists():
        return False
    try:
        manifest = json.loads(tiles_json.read_text(encoding="utf-8"))
        chunks_manifest = json.loads(chunks_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not manifest.get("complete"):
        return False
    tiles = manifest.get("tiles", [])
    chunks = chunks_manifest.get("chunks", [])
    if not tiles or not chunks:
        return False
    if list(tiles) != expected_tiles:
        return False
    tile_files_ok = all((tile_dir / str(tile_name)).exists() for tile_name in tiles)
    chunk_files_ok = all((tile_dir / str(chunk.get("file", ""))).exists() for chunk in chunks)
    return tile_files_ok and chunk_files_ok


def image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def pdf_page_count(path: Path) -> int:
    try:
        from pdf2image import pdfinfo_from_path
    except ImportError as exc:
        raise ImportError(
            "pdf2image is required for PDF rendering. Install with: pip install pixelrag-render[pdf]"
        ) from exc

    info = pdfinfo_from_path(str(path))
    pages = int(info.get("Pages", 0))
    if pages < 1:
        raise RuntimeError(f"Could not detect page count for PDF: {path}")
    return pages


def selected_pdf_pages(path: Path, pages: list[int] | None) -> list[int]:
    if pages is None:
        return list(range(1, pdf_page_count(path) + 1))
    page_count = pdf_page_count(path)
    selected = sorted(set(pages))
    invalid = [page for page in selected if page > page_count]
    if invalid:
        raise SystemExit(f"{path.name}: requested page(s) beyond page count {page_count}: {invalid}")
    return selected


def render_one_pdf_page(
    path: Path,
    page_num: int,
    tile_path: Path,
    dpi: int,
    quality: int,
    render_retries: int,
    fallback_dpi: int,
) -> tuple[int, int]:
    try:
        from pdf2image import convert_from_path
        from PIL import ImageFile
    except ImportError as exc:
        raise ImportError(
            "pdf2image and Pillow are required for PDF rendering. "
            "Install with: pip install pixelrag-render[pdf]"
        ) from exc

    ImageFile.LOAD_TRUNCATED_IMAGES = True
    attempts = max(render_retries, 1)
    dpi_candidates = [dpi]
    if fallback_dpi > 0 and fallback_dpi != dpi:
        dpi_candidates.append(fallback_dpi)
    fmt_candidates = ["jpeg", "png"]
    errors: list[str] = []

    for candidate_dpi in dpi_candidates:
        for fmt in fmt_candidates:
            for attempt in range(1, attempts + 1):
                try:
                    convert_kwargs: dict[str, Any] = {
                        "pdf_path": str(path),
                        "dpi": candidate_dpi,
                        "fmt": fmt,
                        "first_page": page_num,
                        "last_page": page_num,
                        "thread_count": 1,
                    }
                    if fmt == "jpeg":
                        convert_kwargs["jpegopt"] = {"quality": quality, "progressive": True}
                    images = convert_from_path(**convert_kwargs)
                    if not images:
                        raise RuntimeError("pdf2image returned no image")
                    source_image = images[0]
                    output_image = source_image
                    try:
                        source_image.load()
                        if source_image.mode not in {"RGB", "L"}:
                            output_image = source_image.convert("RGB")
                        width, height = output_image.size
                        output_image.save(str(tile_path), "JPEG", quality=quality)
                    finally:
                        if output_image is not source_image:
                            output_image.close()
                        for image in images:
                            image.close()
                    return width, height
                except Exception as exc:
                    errors.append(
                        f"dpi={candidate_dpi}, fmt={fmt}, attempt={attempt}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    if tile_path.exists() and tile_path.stat().st_size == 0:
                        tile_path.unlink(missing_ok=True)
                    time.sleep(min(2.0 * attempt, 10.0))

    raise RuntimeError("; ".join(errors[-6:]))


def append_render_error(output_dir: Path, source_name: str, page_num: int, error: Exception) -> None:
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source_name,
        "page": page_num,
        "error_type": type(error).__name__,
        "error": str(error)[:1000],
    }
    with (output_dir / "render_errors.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def write_pdf_manifests(tile_dir: Path, path: Path, dpi: int, tiles: list[dict[str, Any]]) -> None:
    tile_names = [str(tile["name"]) for tile in tiles]
    chunks = [
        {
            "tile": tile["name"],
            "tile_index": int(tile["tile_index"]),
            "chunk_index": 0,
            "file": tile["name"],
            "x_offset": 0,
            "y_offset": 0,
            "height": int(tile["height"]),
            "width": int(tile["width"]),
            "page_num": int(tile["page_num"]),
        }
        for tile in tiles
    ]
    viewport_width = int(tiles[0]["width"]) if tiles else 0
    tile_height = int(tiles[0]["height"]) if tiles else 0
    (tile_dir / "tiles.json").write_text(
        json.dumps(
            {
                "source": str(path),
                "dpi": dpi,
                "total_pages": len(tile_names),
                "tiles": tile_names,
                "complete": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (tile_dir / "chunks.json").write_text(
        json.dumps(
            {
                "page_height": 0,
                "viewport_width": viewport_width,
                "tile_height": tile_height,
                "chunk_height": tile_height,
                "num_tiles": len(tile_names),
                "num_chunks": len(chunks),
                "chunks": chunks,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def render_pdf_robust(
    path: Path,
    output_dir: Path,
    pages: list[int] | None,
    dpi: int,
    quality: int,
    render_retries: int,
    fallback_dpi: int,
    fail_on_error: bool,
) -> int:
    tile_dir = output_dir / f"{path.stem}.png.tiles"
    tile_dir.mkdir(parents=True, exist_ok=True)

    selected_pages = selected_pdf_pages(path, pages)
    page_offset = 1 if pages is None else min(selected_pages)
    expected_tile_names = [f"tile_{page_num - page_offset:04d}.jpg" for page_num in selected_pages]
    if existing_manifest_complete(tile_dir, expected_tile_names):
        print(f"  Reusing existing complete render: {tile_dir}", flush=True)
        return 0
    saved_tiles: list[dict[str, Any]] = []
    total = len(selected_pages)

    for position, page_num in enumerate(selected_pages, start=1):
        tile_index = page_num - page_offset
        tile_name = f"tile_{tile_index:04d}.jpg"
        tile_path = tile_dir / tile_name
        dimensions = image_dimensions(tile_path) if tile_path.exists() and tile_path.stat().st_size > 0 else None
        if dimensions:
            width, height = dimensions
            status = "reused"
        else:
            if tile_path.exists():
                tile_path.unlink(missing_ok=True)
            try:
                width, height = render_one_pdf_page(
                    path,
                    page_num,
                    tile_path,
                    dpi,
                    quality,
                    render_retries,
                    fallback_dpi,
                )
                status = "rendered"
            except Exception as exc:
                append_render_error(output_dir, path.name, page_num, exc)
                message = f"  page {position}/{total} (PDF page {page_num}) failed: {type(exc).__name__}: {exc}"
                if fail_on_error:
                    raise RuntimeError(message) from exc
                print(f"WARNING: {message}", flush=True)
                continue
        saved_tiles.append(
            {
                "name": tile_name,
                "tile_index": tile_index,
                "page_num": page_num,
                "width": width,
                "height": height,
            }
        )
        print(
            f"  page {position}/{total} (PDF page {page_num}) {status}: "
            f"{tile_name} {width}x{height}",
            flush=True,
        )

    if not saved_tiles:
        raise RuntimeError(f"No pages could be rendered for {path}")
    write_pdf_manifests(tile_dir, path, dpi, saved_tiles)
    return len(saved_tiles)


def render_sources(
    mapping: list[dict[str, str]],
    output_dir: Path,
    pages: list[int] | None,
    dpi: int,
    quality: int,
    render_retries: int,
    fallback_dpi: int,
    fail_on_render_error: bool,
) -> int:

    tiles_dir = output_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    rendered = 0
    articles: list[dict[str, str]] = []

    for index, item in enumerate(mapping, start=1):
        staged = Path(item["staged_path"])
        articles.append({"title": item["original_name"], "url": item["original_path"]})
        print(f"Rendering [{index}/{len(mapping)}] {item['original_name']} ...", flush=True)
        if staged.suffix.lower() == ".pdf":
            render_pdf_robust(
                staged,
                tiles_dir,
                pages=pages,
                dpi=dpi,
                quality=quality,
                render_retries=render_retries,
                fallback_dpi=fallback_dpi,
                fail_on_error=fail_on_render_error,
            )
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


def chunk_tiles(args: argparse.Namespace, output_dir: Path) -> StageResult:
    return run_command(
        [
            sys.executable,
            "-m",
            "pixelrag_embed.chunk",
            "--shard-dir",
            str(output_dir / "tiles"),
            "--workers",
            "1",
        ],
        heartbeat_seconds=args.heartbeat_seconds,
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
    if args.embed_limit > 0:
        command.extend(["--limit", str(args.embed_limit)])
    else:
        print("Embedding all visual chunks; no --limit will be passed to PixelRAG.", flush=True)
    return run_command(command, timeout=args.embed_timeout, heartbeat_seconds=args.heartbeat_seconds)


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


def ensure_collection(args: argparse.Namespace, client: QdrantClient, collection: str, vector_size: int, recreate: bool) -> None:
    if qdrant_call(args, f"{collection}: check collection", client.collection_exists, collection):
        if recreate:
            qdrant_call(args, f"{collection}: delete collection", client.delete_collection, collection, timeout=args.qdrant_timeout)
        else:
            return
    qdrant_call(
        args,
        f"{collection}: create collection",
        client.create_collection,
        collection_name=collection,
        vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
        on_disk_payload=True,
        timeout=args.qdrant_timeout,
    )
    for field, schema in PAYLOAD_INDEXES.items():
        try:
            qdrant_call(
                args,
                f"{collection}: create payload index {field}",
                client.create_payload_index,
                collection_name=collection,
                field_name=field,
                field_schema=schema,
                wait=True,
                timeout=args.qdrant_timeout,
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


def count_embedding_points(output_dir: Path) -> int:
    total = 0
    for shard_path in shard_files(output_dir / "embeddings"):
        with np.load(shard_path) as data:
            total += int(data["embeddings"].shape[0])
    return total


def qdrant_count(args: argparse.Namespace, client: QdrantClient, collection: str) -> int:
    result = qdrant_call(
        args,
        f"{collection}: count points",
        client.count,
        collection_name=collection,
        exact=True,
        timeout=args.qdrant_timeout,
    )
    return int(result.count)


def skip_points(points: Iterable[models.PointStruct], count: int) -> Iterable[models.PointStruct]:
    skipped = 0
    iterator = iter(points)
    while skipped < count:
        try:
            next(iterator)
        except StopIteration:
            return
        skipped += 1
    yield from iterator


def index_qdrant(args: argparse.Namespace, output_dir: Path) -> int:
    vector_size = first_vector_size(output_dir)
    total_points = count_embedding_points(output_dir)
    if args.dry_run:
        print(
            f"Dry run enabled; Qdrant upsert skipped. "
            f"Prepared {total_points} visual point(s) with vector_size={vector_size}.",
            flush=True,
        )
        return 0
    client = QdrantClient(url=args.qdrant_url, api_key=args.qdrant_api_key, timeout=args.qdrant_timeout)
    ensure_collection(args, client, args.collection, vector_size, args.recreate)
    resume_offset = 0
    if not args.recreate and not args.no_resume:
        resume_offset = min(total_points, qdrant_count(args, client, args.collection))
        if resume_offset:
            print(
                f"{args.collection}: resume enabled, skipping {resume_offset}/{total_points} "
                "already stored visual point(s). Use --no-resume to reprocess.",
                flush=True,
            )
        if resume_offset >= total_points:
            print(f"{args.collection}: all {total_points} visual point(s) already indexed.", flush=True)
            return resume_offset
    indexed = resume_offset
    point_iterable = iter_points(output_dir, args.collection)
    if resume_offset:
        point_iterable = skip_points(point_iterable, resume_offset)
    for batch in batched(point_iterable, args.upsert_batch_size):
        qdrant_call(
            args,
            f"{args.collection}: upsert visual batch ending at {indexed + len(batch)}",
            client.upsert,
            collection_name=args.collection,
            points=batch,
            wait=True,
            timeout=args.qdrant_timeout,
        )
        indexed += len(batch)
        print(f"Upserted {indexed}/{total_points} visual point(s) into {args.collection} ...", flush=True)
    return qdrant_count(args, client, args.collection)


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


def check_cuda() -> None:
    import torch

    print(f"torch={torch.__version__}")
    print(f"torch_cuda_version={torch.version.cuda}")
    print(f"cuda_available={torch.cuda.is_available()}")
    print(f"device_count={torch.cuda.device_count()}")
    if torch.cuda.is_available():
        device = torch.device("cuda:0")
        props = torch.cuda.get_device_properties(device)
        print(f"device_name={props.name}")
        print(f"total_memory_gb={props.total_memory / (1024 ** 3):.2f}")
        tensor = torch.rand((256, 256), device=device)
        print(f"tensor_device={tensor.device}")
        print(f"tensor_sum={float(tensor.sum().detach().cpu()):.4f}")


def main() -> None:
    args = parse_args()
    if args.check_cuda:
        check_cuda()
        return

    source = Path(args.source).resolve()
    if not source.exists():
        raise SystemExit(f"Source path does not exist: {source}")
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.force:
        clean_work_dirs(output_dir)

    stages: list[StageResult] = []
    mapping = stage_source(source, output_dir, limit=args.source_limit)
    print(f"Prepared {len(mapping)} source file(s).", flush=True)

    if not args.skip_render:
        print("Stage 1/4: rendering documents into PixelRAG tiles ...", flush=True)
        start = time.time()
        rendered = render_sources(
            mapping,
            output_dir,
            parse_pages(args.pages),
            args.dpi,
            args.quality,
            args.render_retries,
            args.render_fallback_dpi,
            args.fail_on_render_error,
        )
        stages.append(StageResult("render", "ok", round(time.time() - start, 2), f"documents={rendered}, pages={args.pages}"))
        print(f"Render complete: {rendered} document(s).", flush=True)
    else:
        stages.append(StageResult("render", "skipped", 0, "reused existing tiles"))

    print("Stage 2/4: chunking rendered tiles ...", flush=True)
    chunk_result = chunk_tiles(args, output_dir)
    stages.append(StageResult("chunk", chunk_result.status, chunk_result.seconds, chunk_result.note))
    if chunk_result.status != "ok":
        write_report(output_dir, args, stages, 0)
        raise SystemExit(1)
    print("Chunking complete.", flush=True)

    if not args.skip_embed:
        print("Stage 3/4: embedding visual chunks ...", flush=True)
        embed_result = embed_tiles(args, output_dir)
        stages.append(StageResult("embed", embed_result.status, embed_result.seconds, embed_result.note))
        if embed_result.status != "ok":
            write_report(output_dir, args, stages, 0)
            raise SystemExit(1)
        print("Embedding complete.", flush=True)
    else:
        stages.append(StageResult("embed", "skipped", 0, "reused existing shards"))

    print("Stage 4/4: writing PixelRAG visual vectors to Qdrant ...", flush=True)
    indexed = index_qdrant(args, output_dir)
    stages.append(StageResult("qdrant_upsert", "ok", 0, f"points={indexed}"))
    write_report(output_dir, args, stages, indexed)
    print(f"Indexed {indexed} PixelRAG visual point(s) into Qdrant collection {args.collection}.")
    print(f"Report: {output_dir / f'qdrant_{safe_name(args.collection)}_report.md'}")


if __name__ == "__main__":
    main()
