#!/usr/bin/env python3
"""Small PixelRAG-to-Qdrant retrieval smoke test.

This embeds text queries with Qwen3-VL and searches a PixelRAG visual Qdrant
collection. It is intended for quick local checks on small visual indexes.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
from qdrant_client import QdrantClient


DEFAULT_QUERIES = [
    {
        "id": "CEO_SENTIMENT",
        "query": "CEO confidence index declined from 83.0 to 78.5 and expected global GDP growth eased",
        "expected_page": 3,
    },
    {
        "id": "TRANSFORMATION",
        "query": "enterprise-wide transformation 52 percent significant transformation 45 percent begin in 2026",
        "expected_page": 6,
    },
    {
        "id": "AI_FIGURE",
        "query": "AI technologies driving transformation generative AI machine learning agentic AI ranking",
        "expected_page": 9,
    },
    {
        "id": "ACQUISITIONS",
        "query": "CEOs pursuing acquisitions in the next 12 months acquisition objective operational optimization productivity",
        "expected_page": 15,
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test PixelRAG visual retrieval from Qdrant.")
    parser.add_argument("--collection", default="ey_rag_pixelrag_visual_ceo_ai_4page_eval")
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument("--output-dir", default="pixelrag_indexes/ceo_ai_pixelrag_4page_eval")
    parser.add_argument("--model", default="Qwen/Qwen3-VL-Embedding-2B")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu", "auto"])
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--output", default="eval/pixelrag_local_retrieval_smoke.json")
    return parser.parse_args()


def resolve_device(value: str) -> str:
    if value != "auto":
        return value
    import torch

    return "cuda" if torch.cuda.is_available() else "cpu"


def load_page_lookup(output_dir: Path) -> dict[tuple[int, int], int]:
    lookup: dict[tuple[int, int], int] = {}
    tiles_root = output_dir / "tiles"
    for chunks_path in tiles_root.glob("*.png.tiles/chunks.json"):
        article_id = int(chunks_path.parent.name.replace(".png.tiles", ""))
        manifest = json.loads(chunks_path.read_text(encoding="utf-8"))
        for chunk in manifest.get("chunks", []):
            tile_index = int(chunk.get("tile_index", 0))
            page_num = int(chunk.get("page_num", tile_index + 1))
            lookup[(article_id, tile_index)] = page_num
    return lookup


def embed_queries(queries: list[str], model_name: str, device: str) -> np.ndarray:
    import torch
    from transformers import AutoProcessor, Qwen3VLForConditionalGeneration

    device = resolve_device(device)
    dtype = torch.float32 if device == "cpu" else torch.float16
    processor = AutoProcessor.from_pretrained(model_name, trust_remote_code=True)
    model = Qwen3VLForConditionalGeneration.from_pretrained(
        model_name,
        trust_remote_code=True,
        torch_dtype=dtype,
        attn_implementation="sdpa",
    ).eval()
    if device != "cpu":
        model = model.to(device)

    instruction = "Retrieve images or text relevant to the user's query."
    messages = [
        [
            {"role": "system", "content": [{"type": "text", "text": instruction}]},
            {"role": "user", "content": [{"type": "text", "text": query}]},
        ]
        for query in queries
    ]
    texts = [
        processor.apply_chat_template(message, tokenize=False, add_generation_prompt=True)
        for message in messages
    ]
    inputs = processor(text=texts, return_tensors="pt", padding=True)
    inputs = {key: value.to(device) if hasattr(value, "to") else value for key, value in inputs.items()}

    with torch.no_grad():
        outputs = model.model(**inputs)
        last_hidden = outputs.last_hidden_state
        attention_mask = inputs["attention_mask"]
        last_token_indices = attention_mask.sum(dim=1) - 1
        pooled = last_hidden[
            torch.arange(last_hidden.size(0), device=last_hidden.device),
            last_token_indices,
        ]
        pooled = torch.nn.functional.normalize(pooled, p=2, dim=-1)
    return pooled.cpu().float().numpy()


def search(client: QdrantClient, collection: str, vector: list[float], top_k: int) -> list[Any]:
    if hasattr(client, "query_points"):
        result = client.query_points(
            collection_name=collection,
            query=vector,
            limit=top_k,
            with_payload=True,
            with_vectors=False,
        )
        return list(result.points)
    return client.search(
        collection_name=collection,
        query_vector=vector,
        limit=top_k,
        with_payload=True,
        with_vectors=False,
    )


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    page_lookup = load_page_lookup(output_dir)
    query_texts = [item["query"] for item in DEFAULT_QUERIES]

    start = time.perf_counter()
    vectors = embed_queries(query_texts, args.model, args.device)
    embed_seconds = round(time.perf_counter() - start, 2)

    client = QdrantClient(url=args.qdrant_url, timeout=60)
    results: list[dict[str, Any]] = []
    for item, vector in zip(DEFAULT_QUERIES, vectors):
        hits = []
        for hit in search(client, args.collection, vector.astype(np.float32).tolist(), args.top_k):
            payload = dict(hit.payload or {})
            article_id = int(payload.get("article_id", 0))
            tile_index = int(payload.get("tile_index", 0))
            page = page_lookup.get((article_id, tile_index))
            hits.append(
                {
                    "score": float(hit.score),
                    "page": page,
                    "article_id": article_id,
                    "tile_index": tile_index,
                    "chunk_index": int(payload.get("chunk_index", 0)),
                    "tile_path": payload.get("tile_path", ""),
                }
            )
        expected_page = int(item["expected_page"])
        top_page = hits[0]["page"] if hits else None
        results.append(
            {
                **item,
                "top_page": top_page,
                "top1_correct": top_page == expected_page,
                "hits": hits,
            }
        )

    summary = {
        "collection": args.collection,
        "qdrant_url": args.qdrant_url,
        "model": args.model,
        "device": resolve_device(args.device),
        "query_embed_seconds": embed_seconds,
        "top1_accuracy": round(sum(1 for row in results if row["top1_correct"]) / len(results), 3),
        "results": results,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=True), encoding="utf-8")

    print(f"PixelRAG retrieval smoke: {summary['top1_accuracy'] * 100:.1f}% top-1 accuracy")
    print(f"Query embedding time: {embed_seconds}s for {len(results)} queries")
    for row in results:
        hits_text = ", ".join(
            f"p{hit['page']}({hit['score']:.4f})" for hit in row["hits"]
        )
        status = "OK" if row["top1_correct"] else "MISS"
        print(f"{status} {row['id']}: expected p{row['expected_page']}, hits: {hits_text}")
    print(f"Wrote: {output_path}")


if __name__ == "__main__":
    main()
