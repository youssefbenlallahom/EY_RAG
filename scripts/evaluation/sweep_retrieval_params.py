"""Sweep retrieval parameters and choose stable defaults for the benchmark runner."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from generate_retrieval_eval_report import score_result
from run_retrieval_benchmark_qdrant import (
    COLLECTION_BY_INDEXING_TYPE,
    TEXT_INDEXING_TYPES,
    build_extractive_answer,
    collection_exists,
    context_from_candidate,
    embed_text,
    infer_image_paths,
    load_chunk_index,
    merge_candidates,
    qdrant_search,
    rank_candidates,
)


WEIGHT_PROFILES = {
    "balanced": (0.55, 0.25, 0.15, 0.05),
    "bm25_plus": (0.45, 0.35, 0.15, 0.05),
    "vector_plus": (0.65, 0.20, 0.10, 0.05),
    "lexical_plus": (0.45, 0.25, 0.25, 0.05),
    "numeric_plus": (0.50, 0.25, 0.15, 0.10),
}


@dataclass(frozen=True)
class SweepConfig:
    name: str
    top_k: int
    candidate_k: int
    bm25_k: int
    window_radius: int
    context_char_limit: int
    answer_contexts: int
    weight_profile: str
    vector_weight: float
    bm25_weight: float
    lexical_weight: float
    number_weight: float

    @property
    def context_budget(self) -> int:
        return self.top_k * self.context_char_limit * (1 + 2 * self.window_radius)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sweep retrieval params against existing Qdrant indexes.")
    parser.add_argument("--scenarios", default="eval/retrieval_benchmark_scenarios.json")
    parser.add_argument("--chunks-dir", default="chunks")
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--embedding-model", default="bge-m3")
    parser.add_argument("--filtered-images-dir", default="filtered_images")
    parser.add_argument("--output", default="eval/retrieval_param_sweep_results.json")
    parser.add_argument("--best-results-output", default="eval/retrieval_results_qdrant_bge_m3.json")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--top-ks", default="5,8,10")
    parser.add_argument("--pool-pairs", default="20:20,40:40,60:60,80:80,40:80,80:40")
    parser.add_argument("--window-radii", default="1,2,3")
    parser.add_argument("--context-char-limits", default="1400,2500,3500")
    parser.add_argument("--answer-contexts", default="3,5,8")
    parser.add_argument(
        "--weight-profiles",
        default="balanced,bm25_plus,vector_plus,lexical_plus,numeric_plus",
        help="Comma-separated profile names from WEIGHT_PROFILES.",
    )
    parser.add_argument("--indexing-types", default="all")
    parser.add_argument("--max-configs", type=int, default=None)
    parser.add_argument("--write-best-results", action="store_true")
    return parser.parse_args()


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=True)
        handle.write("\n")


def csv_ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def pool_pairs(value: str) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    for item in csv_values(value):
        if ":" in item:
            left, right = item.split(":", 1)
        else:
            left = right = item
        pairs.append((int(left), int(right)))
    return pairs


def resolve_indexing_types(value: str) -> list[str]:
    if value.strip().lower() == "all":
        return list(TEXT_INDEXING_TYPES)
    return csv_values(value)


def build_configs(args: argparse.Namespace) -> list[SweepConfig]:
    configs: list[SweepConfig] = []
    profiles = csv_values(args.weight_profiles)
    for profile in profiles:
        if profile not in WEIGHT_PROFILES:
            raise SystemExit(f"Unknown weight profile: {profile}. Known: {', '.join(WEIGHT_PROFILES)}")

    for top_k in csv_ints(args.top_ks):
        for candidate_k, bm25_k in pool_pairs(args.pool_pairs):
            for window_radius in csv_ints(args.window_radii):
                for context_char_limit in csv_ints(args.context_char_limits):
                    for answer_contexts in csv_ints(args.answer_contexts):
                        if answer_contexts > top_k:
                            continue
                        for profile in profiles:
                            vector_weight, bm25_weight, lexical_weight, number_weight = WEIGHT_PROFILES[profile]
                            name = (
                                f"top{top_k}_cand{candidate_k}_bm25{bm25_k}_"
                                f"win{window_radius}_ctx{context_char_limit}_ans{answer_contexts}_{profile}"
                            )
                            configs.append(
                                SweepConfig(
                                    name=name,
                                    top_k=top_k,
                                    candidate_k=candidate_k,
                                    bm25_k=bm25_k,
                                    window_radius=window_radius,
                                    context_char_limit=context_char_limit,
                                    answer_contexts=answer_contexts,
                                    weight_profile=profile,
                                    vector_weight=vector_weight,
                                    bm25_weight=bm25_weight,
                                    lexical_weight=lexical_weight,
                                    number_weight=number_weight,
                                )
                            )
    if args.max_configs:
        configs = configs[: args.max_configs]
    return configs


def runner_args(config: SweepConfig, args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        top_k=config.top_k,
        candidate_k=config.candidate_k,
        bm25_k=config.bm25_k,
        context_char_limit=config.context_char_limit,
        answer_contexts=config.answer_contexts,
        window_radius=config.window_radius,
        filtered_images_dir=args.filtered_images_dir,
        disable_bm25=False,
        disable_rerank=False,
        vector_weight=config.vector_weight,
        bm25_weight=config.bm25_weight,
        lexical_weight=config.lexical_weight,
        number_weight=config.number_weight,
    )


def build_result(
    scenario: dict[str, Any],
    indexing_type: str,
    collection: str,
    cache_item: dict[str, Any],
    config: SweepConfig,
    args: argparse.Namespace,
) -> dict[str, Any]:
    local_args = runner_args(config, args)
    chunk_index = cache_item["chunk_index"]
    vector_hits = cache_item["vector_hits"][: config.candidate_k]
    bm25_hits = cache_item["bm25_hits"][: config.bm25_k]
    candidates = merge_candidates(vector_hits, bm25_hits, chunk_index)
    ranked = rank_candidates(scenario["question"], candidates, local_args)
    contexts = [
        context_from_candidate(
            candidate,
            rank=index + 1,
            context_char_limit=config.context_char_limit,
            chunk_index=chunk_index,
            window_radius=config.window_radius,
        )
        for index, candidate in enumerate(ranked[: config.top_k])
    ]
    return {
        "scenario_id": scenario["id"],
        "indexing_type": indexing_type,
        "query": scenario["question"],
        "retrieval_status": "ok",
        "retrieved_contexts": contexts,
        "retrieved_answer": build_extractive_answer(contexts, config.answer_contexts),
        "retrieved_image_paths": infer_image_paths(contexts, Path(args.filtered_images_dir)),
        "latency_ms": None,
        "top_k": config.top_k,
        "candidate_k": config.candidate_k,
        "bm25_k": config.bm25_k,
        "retrieval_mode": f"hybrid_vector_bm25_rerank:{config.weight_profile}",
        "window_radius": config.window_radius,
        "context_char_limit": config.context_char_limit,
        "answer_contexts": config.answer_contexts,
        "human_score": None,
        "human_notes": "",
        "collection": collection,
    }


def score_config(
    config: SweepConfig,
    scenarios: list[dict[str, Any]],
    indexing_types: list[str],
    cache: dict[tuple[str, str], dict[str, Any]],
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scores: list[float] = []
    coverage_scores: list[float] = []
    text_scores: list[float] = []
    hybrid_scores: list[float] = []
    low_rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    image_rows = 0
    results: list[dict[str, Any]] = []

    for scenario in scenarios:
        for indexing_type in indexing_types:
            cache_item = cache[(scenario["id"], indexing_type)]
            if cache_item.get("status") != "ok":
                result = {
                    "scenario_id": scenario["id"],
                    "indexing_type": indexing_type,
                    "query": scenario["question"],
                    "retrieval_status": cache_item.get("status"),
                    "known_errors": cache_item.get("known_errors", []),
                    "retrieved_answer": "",
                    "retrieved_image_paths": [],
                    "retrieved_contexts": [],
                    "top_k": config.top_k,
                    "candidate_k": config.candidate_k,
                    "collection": cache_item.get("collection", ""),
                }
            else:
                result = build_result(
                    scenario,
                    indexing_type,
                    cache_item["collection"],
                    cache_item,
                    config,
                    args,
                )
            score = score_result(scenario, result)
            result["_auto_score"] = score["display_score"]
            result["_coverage_score"] = score["coverage_score"]
            results.append(result)
            status_counts[str(result.get("retrieval_status"))] += 1
            scores.append(float(score["display_score"]))
            coverage_scores.append(float(score["coverage_score"]))
            if scenario.get("modality") == "text_only":
                text_scores.append(float(score["display_score"]))
            else:
                hybrid_scores.append(float(score["display_score"]))
            if result.get("retrieved_image_paths"):
                image_rows += 1
            if score["display_score"] < 85:
                low_rows.append(
                    {
                        "scenario_id": scenario["id"],
                        "indexing_type": indexing_type,
                        "score": score["display_score"],
                        "coverage_score": score["coverage_score"],
                    }
                )

    metric = {
        **asdict(config),
        "context_budget": config.context_budget,
        "avg_score": round(statistics.mean(scores), 3) if scores else 0,
        "median_score": round(statistics.median(scores), 3) if scores else 0,
        "min_score": round(min(scores), 3) if scores else 0,
        "p10_score": round(quantile(scores, 0.10), 3) if scores else 0,
        "text_avg": round(statistics.mean(text_scores), 3) if text_scores else 0,
        "hybrid_avg": round(statistics.mean(hybrid_scores), 3) if hybrid_scores else 0,
        "coverage_avg": round(statistics.mean(coverage_scores), 3) if coverage_scores else 0,
        "low_count": len(low_rows),
        "image_rows": image_rows,
        "status_counts": dict(status_counts),
        "low_rows": low_rows[:20],
    }
    return metric, results


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return ordered[index]


def choose_best(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    if not metrics:
        raise SystemExit("No metrics were produced.")
    best_score = max(metric["avg_score"] for metric in metrics)
    near_best = [metric for metric in metrics if metric["avg_score"] >= best_score - 0.1]
    near_best.sort(
        key=lambda metric: (
            -metric["avg_score"],
            metric["low_count"],
            -metric["min_score"],
            metric["context_budget"],
            metric["top_k"],
        )
    )
    return near_best[0]


def main() -> None:
    args = parse_args()
    start = time.time()
    scenarios_doc = load_json(args.scenarios)
    scenarios = scenarios_doc.get("scenarios") or []
    indexing_types = resolve_indexing_types(args.indexing_types)
    configs = build_configs(args)
    if not configs:
        raise SystemExit("No sweep configurations were generated.")

    max_candidate_k = max(config.candidate_k for config in configs)
    max_bm25_k = max(config.bm25_k for config in configs)
    chunk_indexes = {
        indexing_type: load_chunk_index(Path(args.chunks_dir), indexing_type)
        for indexing_type in indexing_types
    }

    cache: dict[tuple[str, str], dict[str, Any]] = {}
    print(
        f"Preparing cache for {len(scenarios)} scenarios, {len(indexing_types)} methods, "
        f"{len(configs)} configs."
    )
    for scenario_index, scenario in enumerate(scenarios, start=1):
        print(f"[{scenario_index}/{len(scenarios)}] Embedding and searching {scenario['id']}")
        vector = embed_text(args.ollama_url, args.embedding_model, scenario["question"], args.timeout)
        for indexing_type in indexing_types:
            collection = COLLECTION_BY_INDEXING_TYPE.get(indexing_type, f"ey_rag_{indexing_type}")
            chunk_index = chunk_indexes[indexing_type]
            if not collection_exists(args.qdrant_url, collection, args.timeout):
                cache[(scenario["id"], indexing_type)] = {
                    "status": "collection_missing",
                    "known_errors": [f"collection_missing: {collection}"],
                    "collection": collection,
                }
                continue
            vector_hits = qdrant_search(args.qdrant_url, collection, vector, max_candidate_k, args.timeout)
            bm25_hits = chunk_index.bm25(scenario["question"], max_bm25_k)
            cache[(scenario["id"], indexing_type)] = {
                "status": "ok",
                "collection": collection,
                "chunk_index": chunk_index,
                "vector_hits": vector_hits,
                "bm25_hits": bm25_hits,
            }

    metrics: list[dict[str, Any]] = []
    best_metric: dict[str, Any] | None = None
    best_results: list[dict[str, Any]] = []
    for index, config in enumerate(configs, start=1):
        metric, results = score_config(config, scenarios, indexing_types, cache, args)
        metrics.append(metric)
        if best_metric is None or choose_best([best_metric, metric]) == metric:
            best_metric = metric
            best_results = results
        if index % 100 == 0 or index == len(configs):
            print(f"Scored {index}/{len(configs)} configs. Current best avg: {choose_best(metrics)['avg_score']}")

    metrics.sort(
        key=lambda metric: (
            metric["avg_score"],
            -metric["low_count"],
            metric["min_score"],
            -metric["context_budget"],
        ),
        reverse=True,
    )
    selected = choose_best(metrics)
    if selected["name"] != (best_metric or {}).get("name"):
        selected_config = next(config for config in configs if config.name == selected["name"])
        _, best_results = score_config(selected_config, scenarios, indexing_types, cache, args)

    output = {
        "run_name": "retrieval-param-sweep",
        "run_date": time.strftime("%Y-%m-%d"),
        "seconds": round(time.time() - start, 2),
        "scenarios": len(scenarios),
        "indexing_types": indexing_types,
        "configs_tested": len(configs),
        "selection_rule": "highest avg_score within 0.1 points, then fewer low rows, higher min score, lower context budget",
        "best_config": selected,
        "top_configs": metrics[:25],
    }
    write_json(args.output, output)
    print(f"Wrote sweep summary: {args.output}")
    print(f"Best config: {selected['name']} avg={selected['avg_score']} low_count={selected['low_count']}")

    if args.write_best_results:
        best_output = {
            "run_name": f"qdrant-{args.embedding_model}-{selected['name']}",
            "run_date": time.strftime("%Y-%m-%d"),
            "notes": "Generated by sweep_retrieval_params.py using the selected best retrieval defaults.",
            "qdrant_url": args.qdrant_url,
            "ollama_url": args.ollama_url,
            "embedding_model": args.embedding_model,
            "indexing_types": indexing_types,
            "retrieval_mode": f"hybrid_vector_bm25_rerank:{selected['weight_profile']}",
            "best_config": selected,
            "results": best_results,
        }
        write_json(args.best_results_output, best_output)
        print(f"Wrote best retrieval results: {args.best_results_output}")


if __name__ == "__main__":
    main()
