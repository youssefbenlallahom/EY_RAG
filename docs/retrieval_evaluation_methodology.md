# Retrieval Evaluation Methodology

This project compares chunking and indexing strategies by running the same gold questions against each index, then comparing the retrieved answer and evidence to a fixed ground truth.

The prepared benchmark is in:

- `eval/retrieval_benchmark_scenarios.json`
- `eval/retrieval_benchmark_scenarios.md`
- `eval/retrieval_results_template.json`

## Why This Benchmark Works

The 20 scenarios are intentionally mixed:

- 10 text-only questions for factual retrieval, numeric precision, tables, and regulatory logic.
- 10 hybrid questions where the correct answer requires image-aware retrieval or image-linked Markdown evidence.

This lets you compare:

- Whether the right chunk was retrieved.
- Whether the answer preserved exact numbers and dates.
- Whether table rows stayed intact after chunking.
- Whether figure descriptions stayed attached to the relevant image/page.
- Whether hybrid retrieval returns the relevant image, not only nearby text.

## Recommended Experiment Shape

For each indexing type, run the exact same 20 questions:

- `langchain_markdown_recursive`
- `llamaindex_semantic`
- `haystack_document_splitter`
- `chonkie_semantic`

Use the same:

- embedding model
- final top-k
- candidate-k
- hybrid BM25 setting
- reranking weights
- adjacent window radius
- prompt template
- answer-generation model
- Qdrant search parameters

Only the chunking/indexing method should change. Otherwise, the comparison will not isolate chunking quality.

Selected defaults after the parameter sweep are `top_k=10`, `candidate_k=20`, `bm25_k=20`, `window_radius=1`, `context_char_limit=4500`, `answer_contexts=5`, and reranking weights `vector=0.45`, `bm25=0.25`, `lexical=0.25`, `numeric=0.05`.

## Retrieval Output Format

After each retrieval run, fill a JSON file like:

```json
{
  "run_name": "qdrant-bge-m3-hybrid-top5",
  "run_date": "2026-06-29",
  "indexing_types": ["langchain_markdown_recursive"],
  "retrieval_mode": "hybrid_vector_bm25_rerank",
  "top_k": 10,
  "candidate_k": 20,
  "bm25_k": 20,
  "window_radius": 1,
  "context_char_limit": 4500,
  "answer_contexts": 5,
  "results": [
    {
      "scenario_id": "T01",
      "indexing_type": "langchain_markdown_recursive",
      "query": "Question used at runtime",
      "retrieved_answer": "Generated answer",
      "retrieved_image_paths": [],
      "retrieved_contexts": [
        {
          "rank": 1,
          "score": 0.82,
          "source_path": "output_clean/ey-gl-ceo-outlook-survey-01-2026.md",
          "page": 3,
          "chunk_id": "optional",
          "text": "Retrieved context excerpt"
        }
      ],
      "latency_ms": 700,
      "top_k": 10,
      "candidate_k": 20,
      "bm25_k": 20,
      "retrieval_mode": "hybrid_vector_bm25_rerank",
      "window_radius": 1,
      "context_char_limit": 4500,
      "answer_contexts": 5,
      "human_score": null,
      "human_notes": ""
    }
  ]
}
```

`human_score` is optional. If provided, it is shown as the displayed score in the PDF while the script still reports the automated score.

## Scoring

The report generator computes a deterministic draft score:

| Component | Points | What It Checks |
| --- | ---: | --- |
| Answer coverage | 40 | Required facts/terms from the scenario are present. |
| Source support | 25 | Retrieved contexts include the expected Markdown source files. |
| Image support | 20 | Hybrid scenarios retrieve the expected filtered image path. Text-only scenarios receive full image-support credit. |
| Precision/no hallucination | 15 | Penalizes empty answers and optionally declared known errors or unsupported claims. |

This score is useful for screening, but final evaluation should still include expert review because a semantically correct answer may not use the exact same wording as the `must_contain` terms.

## Generate The PDF Later

Do not run this until ingestion and retrieval are finished.

```powershell
venv\Scripts\python.exe scripts\evaluation\generate_retrieval_eval_report.py `
  --results eval\your_retrieval_results.json `
  --output output\pdf\retrieval_evaluation_report.pdf `
  --include-ground-truth-images
```

If the venv launcher fails, repair/recreate the venv first, then rerun the command.

## What To Compare In The Final PDF

For each scenario and indexing technique, inspect:

- Did the answer match the ground truth?
- Did the retrieved contexts come from the right document/page/section?
- Did numeric values survive table extraction and chunking?
- For hybrid questions, did the retriever return the relevant image path?
- Did the answer overstate, omit, or blend facts from a neighboring section?
- Did one technique retrieve shorter but more precise context, or longer but noisier context?

The winner is not necessarily the highest average score. For a production RAG system, prioritize the method that gives the most reliable evidence support on hard text-table and hybrid cases.
