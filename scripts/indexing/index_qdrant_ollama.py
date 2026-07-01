#!/usr/bin/env python3
"""Index chunk JSONL files into local Qdrant with Ollama embeddings.

Default assumptions:
    Qdrant: http://localhost:6333
    Ollama: http://localhost:11434
    Ollama embedding model: bge-m3

Each chunking strategy is indexed into its own Qdrant collection:
    ey_rag_langchain_markdown_recursive
    ey_rag_haystack_document_splitter
    ey_rag_chonkie_semantic
    ey_rag_llamaindex_semantic
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from qdrant_client import QdrantClient, models
from urllib3.util.retry import Retry


POINT_NAMESPACE = uuid.UUID("2d6fef7b-9ca9-4fd2-81d9-f60b0bf3b4ef")
SUPPORTED_STRATEGIES = {
    "chonkie_semantic",
    "haystack_document_splitter",
    "langchain_markdown_recursive",
    "llamaindex_semantic",
}
PAYLOAD_INDEXES = {
    "chunk_id": models.PayloadSchemaType.KEYWORD,
    "strategy": models.PayloadSchemaType.KEYWORD,
    "source_name": models.PayloadSchemaType.KEYWORD,
    "parent_id": models.PayloadSchemaType.KEYWORD,
    "section_id": models.PayloadSchemaType.KEYWORD,
    "retrieval_role": models.PayloadSchemaType.KEYWORD,
    "primary_image_path": models.PayloadSchemaType.KEYWORD,
    "image_paths": models.PayloadSchemaType.KEYWORD,
    "page_start": models.PayloadSchemaType.INTEGER,
    "page_end": models.PayloadSchemaType.INTEGER,
    "chunk_sequence": models.PayloadSchemaType.INTEGER,
    "page_type": models.PayloadSchemaType.KEYWORD,
    "content_type": models.PayloadSchemaType.KEYWORD,
    "standard_refs": models.PayloadSchemaType.KEYWORD,
    "heading_path_text": models.PayloadSchemaType.TEXT,
    "token_estimate": models.PayloadSchemaType.INTEGER,
}


@dataclass
class StrategyResult:
    strategy: str
    collection: str
    source_chunks: int
    indexed_points: int
    vector_size: int
    seconds: float
    status: str
    note: str = ""


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Embed each chunking strategy with Ollama and index it into local Qdrant."
    )
    parser.add_argument("--chunks-dir", default="chunks", help="Folder containing strategy subfolders.")
    parser.add_argument("--qdrant-url", default=env_value("QDRANT_URL", default="http://localhost:6333"), help="Qdrant REST URL.")
    parser.add_argument("--qdrant-api-key", default=env_value("QDRANT_API_KEY"), help="Optional Qdrant API key.")
    parser.add_argument("--cloud", action="store_true", help="Use QDRANT_CLOUD_URL and QDRANT_CLOUD_API_KEY from .env.")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base URL.")
    parser.add_argument("--ollama-model", default="bge-m3", help="Ollama embedding model name.")
    parser.add_argument("--collection-prefix", default="ey_rag", help="Qdrant collection prefix.")
    parser.add_argument(
        "--strategies",
        default="all",
        help="Comma-separated strategy folder names, or 'all' for supported text strategies.",
    )
    parser.add_argument(
        "--embed-field",
        default="text_for_embedding",
        choices=["text_for_embedding", "text"],
        help="Chunk field used to generate embeddings.",
    )
    parser.add_argument("--embed-batch-size", type=int, default=16, help="Ollama embedding batch size.")
    parser.add_argument("--upsert-batch-size", type=int, default=64, help="Qdrant upsert batch size.")
    parser.add_argument(
        "--source-name",
        default=None,
        help=(
            "Index only chunks from one source document. Accepts a .md chunk "
            "source_name, a .pdf filename, or a bare document stem."
        ),
    )
    parser.add_argument(
        "--limit-per-strategy",
        type=int,
        default=None,
        help="Index only the first N matching chunks for each strategy. Useful for smoke tests.",
    )
    parser.add_argument(
        "--parallel-strategies",
        action="store_true",
        help="Index strategy collections in parallel. Each worker uses its own Qdrant/Ollama client.",
    )
    parser.add_argument("--max-workers", type=int, default=4, help="Maximum parallel strategy workers.")
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Record failed strategies in the report instead of aborting immediately.",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Delete and recreate each target collection before indexing.",
    )
    parser.add_argument(
        "--no-payload-indexes",
        action="store_true",
        help="Skip metadata payload indexes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check services, model dimension, and chunk files without creating collections.",
    )
    parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout in seconds.")
    parser.add_argument("--qdrant-retries", type=int, default=10, help="Retries for transient Qdrant HTTP/SSL errors.")
    parser.add_argument("--qdrant-backoff", type=float, default=2.0, help="Initial Qdrant retry backoff seconds.")
    parser.add_argument("--qdrant-max-sleep", type=float, default=120.0, help="Maximum Qdrant retry sleep seconds.")
    parser.add_argument("--qdrant-jitter", type=float, default=1.0, help="Random jitter added to Qdrant retry sleeps.")
    parser.add_argument("--no-resume", action="store_true", help="Do not skip already indexed points when rerunning.")
    parser.add_argument("--ollama-retries", type=int, default=5, help="Retries for transient Ollama embedding HTTP errors.")
    parser.add_argument("--ollama-backoff", type=float, default=1.0, help="Ollama retry backoff factor.")
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


def make_retry_session(total: int, backoff: float) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=total,
        connect=total,
        read=total,
        status=total,
        allowed_methods=None,
        status_forcelist=(429, 500, 502, 503, 504),
        backoff_factor=backoff,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def csv_arg(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def strategy_files(chunks_dir: Path, strategies: str) -> list[tuple[str, Path]]:
    requested = None if strategies.strip().lower() == "all" else csv_arg(strategies)
    found: list[tuple[str, Path]] = []
    for path in sorted(chunks_dir.glob("*/chunks.jsonl")):
        strategy = path.parent.name
        if requested is None and strategy not in SUPPORTED_STRATEGIES:
            continue
        if requested is not None and strategy not in requested:
            continue
        found.append((strategy, path))
    return found


def source_filter_values(source_name: str | None) -> set[str]:
    if not source_name:
        return set()
    source_path = Path(source_name)
    name = source_path.name
    stem = source_path.stem if source_path.suffix else name
    return {name, stem, f"{stem}.md", f"{stem}.pdf"}


def chunk_matches_source(chunk: dict[str, Any], allowed_sources: set[str]) -> bool:
    if not allowed_sources:
        return True
    source_name = str(chunk.get("source_name") or "")
    source_file = str(chunk.get("source_file") or "")
    candidates = {Path(source_name).name, Path(source_name).stem}
    if source_file:
        candidates.update({Path(source_file).name, Path(source_file).stem})
    return bool(candidates & allowed_sources)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def count_jsonl(
    path: Path,
    limit: int | None = None,
    allowed_sources: set[str] | None = None,
) -> int:
    allowed_sources = allowed_sources or set()
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if allowed_sources:
                line = line.strip()
                if not line:
                    continue
                if not chunk_matches_source(json.loads(line), allowed_sources):
                    continue
            count += 1
            if limit is not None and count >= limit:
                break
    return count


def read_chunks(
    path: Path,
    limit: int | None = None,
    allowed_sources: set[str] | None = None,
) -> Iterable[dict[str, Any]]:
    allowed_sources = allowed_sources or set()
    yielded = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            chunk = json.loads(line)
            if not chunk_matches_source(chunk, allowed_sources):
                continue
            yield chunk
            yielded += 1
            if limit is not None and yielded >= limit:
                break


class OllamaEmbedder:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: int,
        retries: int,
        backoff: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.session = make_retry_session(retries, backoff)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self.session.post(
            f"{self.base_url}/api/embed",
            json={"model": self.model, "input": texts},
            timeout=self.timeout,
        )
        if response.status_code == 404:
            return [self._embed_legacy(text) for text in texts]
        if response.status_code >= 400:
            detail = response.text[:500]
            raise RuntimeError(
                f"Ollama embedding failed ({response.status_code}). "
                f"Model: {self.model}. Response: {detail}"
            )
        data = response.json()
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise RuntimeError(f"Unexpected Ollama /api/embed response shape: {data.keys()}")
        return embeddings

    def _embed_legacy(self, text: str) -> list[float]:
        response = self.session.post(
            f"{self.base_url}/api/embeddings",
            json={"model": self.model, "prompt": text},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            detail = response.text[:500]
            raise RuntimeError(
                f"Ollama legacy embedding failed ({response.status_code}). "
                f"Model: {self.model}. Response: {detail}"
            )
        embedding = response.json().get("embedding")
        if not isinstance(embedding, list):
            raise RuntimeError("Unexpected Ollama /api/embeddings response shape.")
        return embedding

    def vector_size(self) -> int:
        embedding = self.embed(["embedding dimension probe"])[0]
        return len(embedding)


def collection_name(prefix: str, strategy: str) -> str:
    safe = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in strategy)
    return f"{prefix}_{safe}"


def existing_vector_size(args: argparse.Namespace, client: QdrantClient, collection: str) -> int | None:
    try:
        info = qdrant_call(args, f"{collection}: get collection config", client.get_collection, collection)
        vectors = info.config.params.vectors
        if hasattr(vectors, "size"):
            return int(vectors.size)
        if isinstance(vectors, dict):
            first = next(iter(vectors.values()))
            return int(first.size)
    except Exception:
        return None
    return None


def ensure_collection(
    args: argparse.Namespace,
    client: QdrantClient,
    collection: str,
    vector_size: int,
    recreate: bool,
    create_indexes: bool,
) -> None:
    exists = bool(qdrant_call(args, f"{collection}: check collection", client.collection_exists, collection))
    if exists and recreate:
        qdrant_call(args, f"{collection}: delete collection", client.delete_collection, collection, timeout=args.timeout)
        exists = False

    if exists:
        current_size = existing_vector_size(args, client, collection)
        if current_size is not None and current_size != vector_size:
            raise RuntimeError(
                f"Collection {collection} has vector size {current_size}, "
                f"but Ollama model produces {vector_size}. Use --recreate."
            )
    else:
        qdrant_call(
            args,
            f"{collection}: create collection",
            client.create_collection,
            collection_name=collection,
            vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
            on_disk_payload=True,
            timeout=args.timeout,
        )

    if create_indexes:
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
                    timeout=args.timeout,
                )
            except Exception as exc:
                print(f"Warning: could not create payload index {collection}.{field}: {exc}")


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(POINT_NAMESPACE, chunk_id))


def payload_from_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    heading_path = chunk.get("heading_path") or []
    if not isinstance(heading_path, list):
        heading_path = [str(heading_path)]
    payload = dict(chunk)
    payload["heading_path"] = heading_path
    payload["heading_path_text"] = " > ".join(str(item) for item in heading_path)
    payload["chunk_id"] = str(chunk.get("chunk_id", ""))
    payload["strategy"] = str(chunk.get("strategy", ""))
    payload["source_name"] = str(chunk.get("source_name", ""))
    payload["parent_id"] = str(chunk.get("parent_id", ""))
    payload["section_id"] = str(chunk.get("section_id", ""))
    payload["retrieval_role"] = str(chunk.get("retrieval_role", ""))
    payload["primary_image_path"] = str(chunk.get("primary_image_path", ""))
    payload["image_paths"] = chunk.get("image_paths") or []
    payload["page_start"] = safe_int(chunk.get("page_start"))
    payload["page_end"] = safe_int(chunk.get("page_end"))
    payload["chunk_sequence"] = safe_int(chunk.get("chunk_sequence"))
    payload["is_atomic"] = bool(chunk.get("is_atomic"))
    payload["page_type"] = str(chunk.get("page_type", ""))
    payload["content_type"] = str(chunk.get("content_type", ""))
    payload["standard_refs"] = chunk.get("standard_refs") or []
    return payload


def batched(items: Iterable[dict[str, Any]], batch_size: int) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for item in items:
        batch.append(item)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def qdrant_count(args: argparse.Namespace, client: QdrantClient, collection: str) -> int:
    result = qdrant_call(
        args,
        f"{collection}: count points",
        client.count,
        collection_name=collection,
        exact=True,
        timeout=args.timeout,
    )
    return int(result.count)


def skip_chunks(chunks: Iterable[dict[str, Any]], count: int) -> Iterable[dict[str, Any]]:
    skipped = 0
    iterator = iter(chunks)
    while skipped < count:
        try:
            next(iterator)
        except StopIteration:
            return
        skipped += 1
    yield from iterator


def index_strategy(
    client: QdrantClient,
    embedder: OllamaEmbedder,
    strategy: str,
    path: Path,
    collection: str,
    vector_size: int,
    args: argparse.Namespace,
) -> StrategyResult:
    start = time.time()
    allowed_sources = source_filter_values(args.source_name)
    source_chunks = count_jsonl(path, args.limit_per_strategy, allowed_sources)
    if source_chunks == 0:
        note = "no chunks matched source filter" if allowed_sources else "empty chunks.jsonl"
        return StrategyResult(strategy, collection, 0, 0, vector_size, 0, "skipped", note)

    if args.dry_run:
        return StrategyResult(strategy, collection, source_chunks, 0, vector_size, 0, "dry_run")

    ensure_collection(
        args,
        client,
        collection,
        vector_size,
        recreate=args.recreate,
        create_indexes=not args.no_payload_indexes,
    )

    resume_offset = 0
    if not args.recreate and not args.no_resume and not args.source_name:
        resume_offset = min(source_chunks, qdrant_count(args, client, collection))
        if resume_offset:
            print(
                f"{collection}: resume enabled, skipping {resume_offset}/{source_chunks} "
                "already stored point(s). Use --no-resume to reprocess.",
                flush=True,
            )
        if resume_offset >= source_chunks:
            return StrategyResult(
                strategy,
                collection,
                source_chunks,
                resume_offset,
                vector_size,
                round(time.time() - start, 2),
                "already_indexed",
            )

    indexed = resume_offset
    pending_points: list[models.PointStruct] = []
    chunk_iterable = read_chunks(path, args.limit_per_strategy, allowed_sources)
    if resume_offset:
        chunk_iterable = skip_chunks(chunk_iterable, resume_offset)
    for chunk_batch in batched(
        chunk_iterable,
        args.embed_batch_size,
    ):
        texts = [str(chunk.get(args.embed_field) or chunk.get("text") or "") for chunk in chunk_batch]
        vectors = embedder.embed(texts)
        for chunk, vector in zip(chunk_batch, vectors):
            chunk_id = str(chunk.get("chunk_id") or f"{strategy}:{indexed}")
            pending_points.append(
                models.PointStruct(
                    id=point_id(chunk_id),
                    vector=vector,
                    payload=payload_from_chunk(chunk),
                )
            )
            if len(pending_points) >= args.upsert_batch_size:
                qdrant_call(
                    args,
                    f"{collection}: upsert batch ending at {indexed + len(pending_points)}",
                    client.upsert,
                    collection_name=collection,
                    points=pending_points,
                    wait=True,
                    timeout=args.timeout,
                )
                indexed += len(pending_points)
                pending_points = []
                print(f"{collection}: indexed {indexed}/{source_chunks}", flush=True)

    if pending_points:
        qdrant_call(
            args,
            f"{collection}: upsert final batch ending at {indexed + len(pending_points)}",
            client.upsert,
            collection_name=collection,
            points=pending_points,
            wait=True,
            timeout=args.timeout,
        )
        indexed += len(pending_points)
        print(f"{collection}: indexed {indexed}/{source_chunks}", flush=True)

    stored_count = qdrant_count(args, client, collection)
    return StrategyResult(
        strategy=strategy,
        collection=collection,
        source_chunks=source_chunks,
        indexed_points=int(stored_count),
        vector_size=vector_size,
        seconds=round(time.time() - start, 2),
        status="indexed",
    )


def write_report(results: list[StrategyResult], chunks_dir: Path, args: argparse.Namespace) -> None:
    report_md = chunks_dir / "qdrant_indexing_report.md"
    report_csv = chunks_dir / "qdrant_indexing_report.csv"
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    with report_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(StrategyResult.__dataclass_fields__.keys()))
        writer.writeheader()
        for result in results:
            writer.writerow(result.__dict__)

    lines = [
        "# Qdrant Indexing Report",
        "",
        f"Generated: {generated}",
        "",
        f"- Qdrant URL: `{args.qdrant_url}`",
        f"- Ollama URL: `{args.ollama_url}`",
        f"- Ollama model: `{args.ollama_model}`",
        f"- Embed field: `{args.embed_field}`",
        f"- Source filter: `{args.source_name or 'all'}`",
        f"- Parallel strategies: `{args.parallel_strategies}`",
        "",
        "| Strategy | Collection | Source chunks | Indexed points | Vector size | Seconds | Status | Note |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for result in results:
        lines.append(
            f"| {result.strategy} | `{result.collection}` | {result.source_chunks} | "
            f"{result.indexed_points} | {result.vector_size} | {result.seconds} | "
            f"{result.status} | {result.note} |"
        )
    report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_qdrant(args: argparse.Namespace, client: QdrantClient, qdrant_url: str) -> None:
    try:
        qdrant_call(args, "qdrant health check", client.get_collections)
    except Exception as exc:
        raise SystemExit(
            f"Qdrant is not reachable at {qdrant_url}.\n"
            "Start Docker Desktop, then run:\n"
            "  docker compose up -d qdrant\n"
            f"Original error: {type(exc).__name__}: {exc}"
        ) from exc


def failure_result(
    strategy: str,
    collection: str,
    path: Path,
    vector_size: int,
    args: argparse.Namespace,
    start: float,
    exc: Exception,
) -> StrategyResult:
    try:
        source_chunks = count_jsonl(
            path,
            args.limit_per_strategy,
            source_filter_values(args.source_name),
        )
    except Exception:
        source_chunks = 0
    note = f"{type(exc).__name__}: {str(exc)[:300]}".replace("|", "/")
    return StrategyResult(
        strategy=strategy,
        collection=collection,
        source_chunks=source_chunks,
        indexed_points=0,
        vector_size=vector_size,
        seconds=round(time.time() - start, 2),
        status="failed",
        note=note,
    )


def run_strategy(
    strategy: str,
    path: Path,
    collection: str,
    vector_size: int,
    args: argparse.Namespace,
    shared_client: QdrantClient | None = None,
    shared_embedder: OllamaEmbedder | None = None,
) -> StrategyResult:
    start = time.time()
    try:
        client = shared_client or QdrantClient(
            url=args.qdrant_url,
            api_key=args.qdrant_api_key,
            timeout=args.timeout,
            check_compatibility=not args.dry_run,
        )
        if shared_client is None and not args.dry_run:
            check_qdrant(args, client, args.qdrant_url)
        embedder = shared_embedder or OllamaEmbedder(
            args.ollama_url,
            args.ollama_model,
            args.timeout,
            args.ollama_retries,
            args.ollama_backoff,
        )
        return index_strategy(client, embedder, strategy, path, collection, vector_size, args)
    except Exception as exc:
        if args.continue_on_error:
            return failure_result(strategy, collection, path, vector_size, args, start, exc)
        raise


def main() -> None:
    args = parse_args()
    chunks_dir = Path(args.chunks_dir)
    strategy_paths = strategy_files(chunks_dir, args.strategies)
    if not strategy_paths:
        raise SystemExit(f"No chunk files found under {chunks_dir}.")

    client = QdrantClient(
        url=args.qdrant_url,
        api_key=args.qdrant_api_key,
        timeout=args.timeout,
        check_compatibility=not args.dry_run,
    )
    if not args.dry_run:
        check_qdrant(args, client, args.qdrant_url)

    embedder = OllamaEmbedder(
        args.ollama_url,
        args.ollama_model,
        args.timeout,
        args.ollama_retries,
        args.ollama_backoff,
    )
    vector_size = embedder.vector_size()
    print(f"Ollama model {args.ollama_model} vector size: {vector_size}")

    results: list[StrategyResult] = []
    if args.parallel_strategies:
        workers = max(1, min(args.max_workers, len(strategy_paths)))
        print(f"Indexing {len(strategy_paths)} strategies in parallel with {workers} workers.")
        indexed_results: list[tuple[int, StrategyResult]] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for order, (strategy, path) in enumerate(strategy_paths):
                collection = collection_name(args.collection_prefix, strategy)
                print(f"\nStrategy: {strategy}\nCollection: {collection}\nChunks: {path}")
                future = executor.submit(
                    run_strategy,
                    strategy,
                    path,
                    collection,
                    vector_size,
                    args,
                )
                futures[future] = order

            for future in as_completed(futures):
                order = futures[future]
                result = future.result()
                indexed_results.append((order, result))
                print(f"{result.strategy}: {result.status} ({result.indexed_points} indexed)")

        results = [result for _, result in sorted(indexed_results, key=lambda item: item[0])]
    else:
        for strategy, path in strategy_paths:
            collection = collection_name(args.collection_prefix, strategy)
            print(f"\nStrategy: {strategy}\nCollection: {collection}\nChunks: {path}")
            result = run_strategy(
                strategy,
                path,
                collection,
                vector_size,
                args,
                shared_client=client,
                shared_embedder=embedder,
            )
            results.append(result)
            print(f"{strategy}: {result.status} ({result.indexed_points} indexed)")

    write_report(results, chunks_dir, args)
    print(f"\nWrote report to {chunks_dir / 'qdrant_indexing_report.md'}")


if __name__ == "__main__":
    main()
