# PixelRAG Visual Index Notes

PixelRAG is different from the text chunking methods in this project. Instead
of parsing documents into text chunks, it renders documents as screenshots,
chunks the screenshot tiles, embeds those visual tiles, and builds a FAISS
index.

For this EY corpus, PixelRAG is most useful as a complementary retrieval path
for pages where layout matters:

- financial statement tables
- charts and dashboards
- slide pages
- dense disclosure checklists
- PDF pages where text extraction loses spatial relationships

## Prepared Wrapper

The wrapper script is:

```powershell
venv\Scripts\python.exe scripts\indexing\build_pixelrag_visual_index.py
```

By default it creates:

```text
pixelrag_indexes/ey_visual_index/pixelrag.yaml
```

The wrapper stages inputs under numeric filenames in `<index>/_source/` and
writes `<index>/_source_mapping.json`. This avoids a current PixelRAG local
source quirk where non-numeric document IDs can break the pipeline.

PixelRAG declares Python 3.12+ and `pixelrag[index]` pulls heavier visual
embedding dependencies such as PyTorch, Transformers, and FAISS.

To smoke-test only a few source PDFs:

```powershell
venv\Scripts\python.exe scripts\indexing\build_pixelrag_visual_index.py --build --limit 1
```

Full build:

```powershell
venv\Scripts\python.exe scripts\indexing\build_pixelrag_visual_index.py --build --force
```

## Notes

PixelRAG uses its own FAISS visual index, not Qdrant. Keep it separate from the
Qdrant/Ollama text indexes and compare it as a visual retrieval baseline.

## References

- [PixelRAG repository](https://github.com/StarTrail-org/PixelRAG)
- [PixelRAG README](https://github.com/StarTrail-org/PixelRAG#readme)
- [PixelRAG pyproject](https://raw.githubusercontent.com/StarTrail-org/PixelRAG/main/pyproject.toml)
