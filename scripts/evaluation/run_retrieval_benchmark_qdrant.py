"""Run the retrieval benchmark against local Qdrant collections.

The output follows eval/retrieval_results_template.json and can be passed to
generate_retrieval_eval_report.py.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


TEXT_INDEXING_TYPES = [
    "langchain_markdown_recursive",
    "llamaindex_semantic",
    "haystack_document_splitter",
    "chonkie_semantic",
]

PIXELRAG_INDEXING_TYPE = "pixelrag_visual_qdrant"

COLLECTION_BY_INDEXING_TYPE = {
    "langchain_markdown_recursive": "ey_rag_langchain_markdown_recursive",
    "llamaindex_semantic": "ey_rag_llamaindex_semantic",
    "haystack_document_splitter": "ey_rag_haystack_document_splitter",
    "chonkie_semantic": "ey_rag_chonkie_semantic",
    "pixelrag_visual_qdrant": "ey_rag_pixelrag_visual",
}

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[./_-][a-z0-9]+)*")
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?%?\b")
STOPWORDS = {
    "a",
    "about",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "did",
    "do",
    "does",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "say",
    "the",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run retrieval scenarios against Qdrant.")
    parser.add_argument("--scenarios", default="eval/retrieval_benchmark_scenarios.json")
    parser.add_argument("--output", default="eval/retrieval_results_qdrant_bge_m3.json")
    parser.add_argument("--chunks-dir", default="chunks")
    parser.add_argument("--qdrant-url", default=env_value("QDRANT_URL", default="http://localhost:6333"))
    parser.add_argument("--qdrant-api-key", default=env_value("QDRANT_API_KEY"))
    parser.add_argument("--cloud", action="store_true", help="Use QDRANT_CLOUD_URL and QDRANT_CLOUD_API_KEY from .env.")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--embedding-model", default="bge-m3")
    parser.add_argument("--scenario-ids", default="", help="Comma-separated scenario IDs to run, for example T01,T02.")
    parser.add_argument("--scenario-limit", type=int, default=None, help="Run only the first N scenarios after filtering.")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument("--bm25-k", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--filtered-images-dir", default="filtered_images")
    parser.add_argument("--indexing-types", default="all")
    parser.add_argument("--include-pixelrag", action="store_true", help="Include PixelRAG in this run.")
    parser.add_argument("--context-char-limit", type=int, default=4500)
    parser.add_argument("--answer-contexts", type=int, default=5)
    parser.add_argument("--window-radius", type=int, default=1)
    parser.add_argument("--disable-bm25", action="store_true")
    parser.add_argument("--disable-rerank", action="store_true")
    parser.add_argument("--vector-weight", type=float, default=0.45)
    parser.add_argument("--bm25-weight", type=float, default=0.25)
    parser.add_argument("--lexical-weight", type=float, default=0.25)
    parser.add_argument("--number-weight", type=float, default=0.05)
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


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)
        handle.write("\n")


def csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_indexing_types(args: argparse.Namespace, scenarios_doc: dict[str, Any]) -> list[str]:
    if args.indexing_types == "all":
        indexing_types = list(TEXT_INDEXING_TYPES)
    elif args.indexing_types == "scenario":
        indexing_types = list(scenarios_doc.get("indexing_types_to_compare") or TEXT_INDEXING_TYPES)
    else:
        indexing_types = csv_values(args.indexing_types)

    if not args.include_pixelrag:
        indexing_types = [name for name in indexing_types if name != PIXELRAG_INDEXING_TYPE]
    return indexing_types


def http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 120,
    api_key: str | None = None,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if api_key:
        headers["api-key"] = api_key
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def collection_exists(qdrant_url: str, collection: str, timeout: int, api_key: str | None = None) -> bool:
    try:
        http_json("GET", f"{qdrant_url.rstrip('/')}/collections/{collection}", timeout=timeout, api_key=api_key)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def embed_text(ollama_url: str, model: str, text: str, timeout: int) -> list[float]:
    url = f"{ollama_url.rstrip('/')}/api/embed"
    try:
        response = http_json("POST", url, {"model": model, "input": [text]}, timeout=timeout)
        embeddings = response.get("embeddings")
        if not embeddings:
            raise RuntimeError("Ollama /api/embed returned no embeddings.")
        return embeddings[0]
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise

    legacy = http_json(
        "POST",
        f"{ollama_url.rstrip('/')}/api/embeddings",
        {"model": model, "prompt": text},
        timeout=timeout,
    )
    embedding = legacy.get("embedding")
    if not embedding:
        raise RuntimeError("Ollama /api/embeddings returned no embedding.")
    return embedding


def qdrant_search(
    qdrant_url: str,
    collection: str,
    vector: list[float],
    top_k: int,
    timeout: int,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    response = http_json(
        "POST",
        f"{qdrant_url.rstrip('/')}/collections/{collection}/points/search",
        {
            "vector": vector,
            "limit": top_k,
            "with_payload": True,
            "with_vector": False,
        },
        timeout=timeout,
        api_key=api_key,
    )
    return response.get("result") or []


def first_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def truncate(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 18].rstrip() + " ... [truncated]"


def tokenize(text: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(str(text or "").lower()) if token not in STOPWORDS]


def numeric_tokens(text: str) -> set[str]:
    return {match.group(0).lower() for match in NUMBER_RE.finditer(str(text or ""))}


def chunk_text_for_scoring(payload: dict[str, Any]) -> str:
    heading = payload.get("heading_path") or []
    if isinstance(heading, list):
        heading_text = " ".join(str(item) for item in heading)
    else:
        heading_text = str(heading)
    return "\n".join(
        [
            str(payload.get("context_prefix") or ""),
            heading_text,
            str(payload.get("text_for_embedding") or payload.get("text") or ""),
        ]
    )


class LocalChunkIndex:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self.by_id: dict[str, dict[str, Any]] = {}
        self.tokens_by_id: dict[str, list[str]] = {}
        self.doc_freq: Counter[str] = Counter()
        self.avg_doc_len = 1.0

        for chunk in chunks:
            chunk_id = str(chunk.get("chunk_id") or "")
            if not chunk_id:
                continue
            self.by_id[chunk_id] = chunk

        self._ensure_window_links()
        doc_lengths: list[int] = []
        for chunk_id, chunk in self.by_id.items():
            tokens = tokenize(chunk_text_for_scoring(chunk))
            self.tokens_by_id[chunk_id] = tokens
            doc_lengths.append(len(tokens))
            self.doc_freq.update(set(tokens))
        if doc_lengths:
            self.avg_doc_len = sum(doc_lengths) / len(doc_lengths)

    def _ensure_window_links(self) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for chunk in self.by_id.values():
            key = str(chunk.get("parent_id") or f"{chunk.get('source_name')}:{chunk.get('page_start')}")
            grouped.setdefault(key, []).append(chunk)

        for group in grouped.values():
            group.sort(key=lambda item: first_int(item.get("chunk_sequence")) or 0)
            for index, chunk in enumerate(group):
                chunk.setdefault("previous_chunk_id", str(group[index - 1].get("chunk_id")) if index > 0 else "")
                chunk.setdefault(
                    "next_chunk_id",
                    str(group[index + 1].get("chunk_id")) if index + 1 < len(group) else "",
                )

    @property
    def doc_count(self) -> int:
        return len(self.by_id)

    def bm25(self, query: str, limit: int) -> list[tuple[str, float]]:
        if not self.by_id or limit <= 0:
            return []

        query_terms = tokenize(query)
        if not query_terms:
            return []

        query_counts = Counter(query_terms)
        k1 = 1.5
        b = 0.75
        scores: list[tuple[str, float]] = []
        for chunk_id, tokens in self.tokens_by_id.items():
            if not tokens:
                continue
            term_counts = Counter(tokens)
            doc_len = len(tokens)
            score = 0.0
            for term, query_count in query_counts.items():
                freq = term_counts.get(term, 0)
                if freq <= 0:
                    continue
                df = self.doc_freq.get(term, 0)
                idf = math.log(1 + (self.doc_count - df + 0.5) / (df + 0.5))
                denom = freq + k1 * (1 - b + b * doc_len / self.avg_doc_len)
                score += idf * ((freq * (k1 + 1)) / denom) * query_count
            if score > 0:
                scores.append((chunk_id, score))
        scores.sort(key=lambda item: item[1], reverse=True)
        return scores[:limit]

    def window_payloads(self, chunk_id: str, radius: int) -> list[dict[str, Any]]:
        center = self.by_id.get(chunk_id)
        if not center:
            return []

        before: list[dict[str, Any]] = []
        cursor = str(center.get("previous_chunk_id") or "")
        for _ in range(max(0, radius)):
            previous = self.by_id.get(cursor)
            if not previous:
                break
            before.append(previous)
            cursor = str(previous.get("previous_chunk_id") or "")

        after: list[dict[str, Any]] = []
        cursor = str(center.get("next_chunk_id") or "")
        for _ in range(max(0, radius)):
            following = self.by_id.get(cursor)
            if not following:
                break
            after.append(following)
            cursor = str(following.get("next_chunk_id") or "")

        return list(reversed(before)) + [center] + after


def load_chunk_index(chunks_dir: Path, indexing_type: str) -> LocalChunkIndex:
    path = chunks_dir / indexing_type / "chunks.jsonl"
    if not path.exists():
        return LocalChunkIndex([])

    chunks: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            chunks.append(json.loads(line))
    return LocalChunkIndex(chunks)


def normalize_scores(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    max_value = max(values.values())
    if max_value <= 0:
        return {key: 0.0 for key in values}
    return {key: value / max_value for key, value in values.items()}


def lexical_score(query: str, payload: dict[str, Any]) -> float:
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return 0.0
    payload_tokens = set(tokenize(chunk_text_for_scoring(payload)))
    return len(query_tokens & payload_tokens) / len(query_tokens)


def number_score(query: str, payload: dict[str, Any]) -> float:
    query_numbers = numeric_tokens(query)
    if not query_numbers:
        return 0.0
    payload_numbers = numeric_tokens(chunk_text_for_scoring(payload))
    return len(query_numbers & payload_numbers) / len(query_numbers)


def merge_candidates(
    vector_hits: list[dict[str, Any]],
    bm25_hits: list[tuple[str, float]],
    chunk_index: LocalChunkIndex,
) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for hit in vector_hits:
        payload = dict(hit.get("payload") or {})
        chunk_id = str(payload.get("chunk_id") or hit.get("id") or "")
        if not chunk_id:
            continue
        local_payload = chunk_index.by_id.get(chunk_id)
        if local_payload:
            payload.update(local_payload)
        candidates[chunk_id] = {
            "chunk_id": chunk_id,
            "payload": payload,
            "vector_score": float(hit.get("score") or 0.0),
            "bm25_score": 0.0,
        }

    for chunk_id, bm25_score in bm25_hits:
        payload = dict(chunk_index.by_id.get(chunk_id) or {})
        if not payload:
            continue
        candidate = candidates.setdefault(
            chunk_id,
            {
                "chunk_id": chunk_id,
                "payload": payload,
                "vector_score": 0.0,
                "bm25_score": 0.0,
            },
        )
        candidate["bm25_score"] = float(bm25_score)
    return candidates


def rank_candidates(
    query: str,
    candidates: dict[str, dict[str, Any]],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    if not candidates:
        return []

    vector_norm = normalize_scores(
        {chunk_id: candidate["vector_score"] for chunk_id, candidate in candidates.items()}
    )
    bm25_norm = normalize_scores(
        {chunk_id: candidate["bm25_score"] for chunk_id, candidate in candidates.items()}
    )

    ranked: list[dict[str, Any]] = []
    for chunk_id, candidate in candidates.items():
        payload = candidate["payload"]
        lex = lexical_score(query, payload)
        nums = number_score(query, payload)
        if args.disable_rerank:
            rerank_score = vector_norm.get(chunk_id, 0.0) + bm25_norm.get(chunk_id, 0.0)
        else:
            rerank_score = (
                args.vector_weight * vector_norm.get(chunk_id, 0.0)
                + args.bm25_weight * bm25_norm.get(chunk_id, 0.0)
                + args.lexical_weight * lex
                + args.number_weight * nums
            )
        enriched = dict(candidate)
        enriched["rerank_score"] = rerank_score
        enriched["lexical_score"] = lex
        enriched["number_score"] = nums
        ranked.append(enriched)

    ranked.sort(
        key=lambda item: (
            item.get("rerank_score", 0.0),
            item.get("vector_score", 0.0),
            item.get("bm25_score", 0.0),
        ),
        reverse=True,
    )
    return ranked


def expanded_text(
    payload: dict[str, Any],
    chunk_index: LocalChunkIndex,
    window_radius: int,
) -> str:
    chunk_id = str(payload.get("chunk_id") or "")
    window = chunk_index.window_payloads(chunk_id, window_radius)
    if not window:
        return str(payload.get("text") or payload.get("text_for_embedding") or "")

    pieces: list[str] = []
    for item in window:
        marker = "current"
        if item.get("chunk_id") != chunk_id:
            marker = "neighbor"
        page = item.get("page_start")
        pieces.append(f"[{marker}, page {page}]\n{item.get('text') or item.get('text_for_embedding') or ''}")
    return "\n\n".join(pieces)


def context_from_candidate(
    candidate: dict[str, Any],
    rank: int,
    context_char_limit: int,
    chunk_index: LocalChunkIndex,
    window_radius: int,
) -> dict[str, Any]:
    payload = candidate.get("payload") or {}
    page_start = first_int(payload.get("page_start"))
    page_end = first_int(payload.get("page_end"))
    source_path = payload.get("source_file") or payload.get("source_name") or ""
    text = expanded_text(payload, chunk_index, window_radius)
    image_paths = payload.get("image_paths") or []
    if isinstance(image_paths, str):
        image_paths = [image_paths] if image_paths else []
    return {
        "rank": rank,
        "score": candidate.get("rerank_score"),
        "vector_score": candidate.get("vector_score"),
        "bm25_score": candidate.get("bm25_score"),
        "lexical_score": candidate.get("lexical_score"),
        "number_score": candidate.get("number_score"),
        "source_path": source_path,
        "page": page_start,
        "page_end": page_end,
        "chunk_id": payload.get("chunk_id"),
        "parent_id": payload.get("parent_id"),
        "section_id": payload.get("section_id"),
        "retrieval_role": payload.get("retrieval_role"),
        "page_type": payload.get("page_type"),
        "content_type": payload.get("content_type"),
        "heading_path": payload.get("heading_path"),
        "image_paths": image_paths,
        "primary_image_path": payload.get("primary_image_path") or (image_paths[0] if image_paths else ""),
        "text": truncate(text, context_char_limit),
    }


def build_extractive_answer(contexts: list[dict[str, Any]], answer_contexts: int) -> str:
    if not contexts:
        return ""
    pieces = ["Retrieved response (extractive, from reranked contexts):"]
    for context in contexts[:answer_contexts]:
        source = Path(str(context.get("source_path") or "")).name
        page = context.get("page")
        score = context.get("score")
        heading = context.get("heading_path") or []
        if isinstance(heading, list):
            heading_text = " > ".join(str(item) for item in heading)
        else:
            heading_text = str(heading or "")
        label_parts = [f"rank {context.get('rank')}"]
        if source:
            label_parts.append(source)
        if page is not None:
            label_parts.append(f"page {page}")
        if score is not None:
            label_parts.append(f"score {score:.4f}" if isinstance(score, float) else f"score {score}")
        if heading_text:
            label_parts.append(heading_text)
        pieces.append(f"\n[{', '.join(label_parts)}]\n{context.get('text', '')}")
    return "\n".join(pieces).strip()


def infer_image_paths(
    contexts: list[dict[str, Any]],
    filtered_images_dir: Path,
) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()

    def add_path(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            paths.append(value)

    for context in contexts:
        for image_path in context.get("image_paths") or []:
            add_path(str(image_path))
        add_path(str(context.get("primary_image_path") or ""))

        page_type = str(context.get("page_type") or "")
        content_type = str(context.get("content_type") or "")
        if page_type != "image_extraction" and content_type not in {"figure", "figure_table"}:
            continue

        source_path = Path(str(context.get("source_path") or ""))
        if not source_path.name:
            continue
        stem = source_path.stem
        for page in range(
            first_int(context.get("page")) or 0,
            (first_int(context.get("page_end")) or first_int(context.get("page")) or 0) + 1,
        ):
            if page <= 0:
                continue
            pattern = f"{stem}__p{page}__*.png"
            for match in sorted(filtered_images_dir.glob(pattern)):
                add_path(match.as_posix())
    return paths


def run_scenario(
    scenario: dict[str, Any],
    indexing_type: str,
    collection: str,
    vector: list[float],
    chunk_index: LocalChunkIndex,
    args: argparse.Namespace,
) -> dict[str, Any]:
    start = time.perf_counter()
    result_base = {
        "scenario_id": scenario["id"],
        "indexing_type": indexing_type,
        "query": scenario["question"],
        "retrieved_answer": "",
        "retrieved_image_paths": [],
        "retrieved_contexts": [],
        "latency_ms": None,
        "top_k": args.top_k,
        "candidate_k": args.candidate_k,
        "retrieval_mode": "hybrid_vector_bm25_rerank" if not args.disable_bm25 else "vector_rerank",
        "window_radius": args.window_radius,
        "human_score": None,
        "human_notes": "",
        "collection": collection,
    }

    if not collection_exists(args.qdrant_url, collection, args.timeout, args.qdrant_api_key):
        result_base.update(
            {
                "retrieval_status": "collection_missing",
                "known_errors": [f"collection_missing: {collection}"],
                "latency_ms": round((time.perf_counter() - start) * 1000),
            }
        )
        return result_base

    try:
        vector_hits = qdrant_search(
            args.qdrant_url,
            collection,
            vector,
            max(args.candidate_k, args.top_k),
            args.timeout,
            args.qdrant_api_key,
        )
        bm25_hits: list[tuple[str, float]] = []
        if not args.disable_bm25:
            bm25_hits = chunk_index.bm25(scenario["question"], args.bm25_k)

        candidates = merge_candidates(vector_hits, bm25_hits, chunk_index)
        ranked = rank_candidates(scenario["question"], candidates, args)
        contexts = [
            context_from_candidate(
                candidate,
                rank=index + 1,
                context_char_limit=args.context_char_limit,
                chunk_index=chunk_index,
                window_radius=args.window_radius,
            )
            for index, candidate in enumerate(ranked[: args.top_k])
        ]
        result_base.update(
            {
                "retrieval_status": "ok",
                "retrieved_contexts": contexts,
                "retrieved_answer": build_extractive_answer(contexts, args.answer_contexts),
                "retrieved_image_paths": infer_image_paths(contexts, Path(args.filtered_images_dir)),
                "latency_ms": round((time.perf_counter() - start) * 1000),
            }
        )
        return result_base
    except Exception as exc:
        result_base.update(
            {
                "retrieval_status": "failed",
                "known_errors": [f"{type(exc).__name__}: {exc}"],
                "latency_ms": round((time.perf_counter() - start) * 1000),
            }
        )
        return result_base


def main() -> None:
    args = parse_args()
    scenarios_doc = load_json(args.scenarios)
    scenarios = scenarios_doc.get("scenarios") or []
    if args.scenario_ids:
        wanted_ids = set(csv_values(args.scenario_ids))
        scenarios = [scenario for scenario in scenarios if str(scenario.get("id")) in wanted_ids]
    if args.scenario_limit is not None:
        scenarios = scenarios[: args.scenario_limit]
    if not scenarios:
        raise SystemExit("No scenarios selected.")
    indexing_types = resolve_indexing_types(args, scenarios_doc)
    chunk_indexes = {
        indexing_type: load_chunk_index(Path(args.chunks_dir), indexing_type)
        for indexing_type in indexing_types
        if indexing_type != PIXELRAG_INDEXING_TYPE
    }

    results: list[dict[str, Any]] = []
    print(f"Running {len(scenarios)} scenario(s) across {len(indexing_types)} indexing type(s).")
    for scenario_index, scenario in enumerate(scenarios, start=1):
        print(f"[{scenario_index}/{len(scenarios)}] Embedding {scenario['id']}: {scenario['question'][:80]}")
        vector = embed_text(args.ollama_url, args.embedding_model, scenario["question"], args.timeout)
        for indexing_type in indexing_types:
            collection = COLLECTION_BY_INDEXING_TYPE.get(indexing_type, f"ey_rag_{indexing_type}")
            chunk_index = chunk_indexes.get(indexing_type, LocalChunkIndex([]))
            print(
                f"  - {indexing_type} -> {collection} "
                f"(local chunks: {chunk_index.doc_count}, candidate_k: {args.candidate_k})"
            )
            result = run_scenario(scenario, indexing_type, collection, vector, chunk_index, args)
            results.append(result)

    output = {
        "run_name": f"qdrant-{args.embedding_model}-hybrid-top{args.top_k}",
        "run_date": datetime.now().strftime("%Y-%m-%d"),
        "notes": (
            "Automatically generated retrieval benchmark results. Retrieval uses Qdrant vector "
            "candidates, optional local BM25 candidates, lightweight reranking, and extractive "
            "answers from the top retrieved contexts."
        ),
        "qdrant_url": args.qdrant_url,
        "ollama_url": args.ollama_url,
        "embedding_model": args.embedding_model,
        "indexing_types": indexing_types,
        "retrieval_mode": "hybrid_vector_bm25_rerank" if not args.disable_bm25 else "vector_rerank",
        "candidate_k": args.candidate_k,
        "bm25_k": args.bm25_k,
        "window_radius": args.window_radius,
        "results": results,
    }
    write_json(args.output, output)
    print(f"Wrote retrieval results: {args.output}")


if __name__ == "__main__":
    main()
