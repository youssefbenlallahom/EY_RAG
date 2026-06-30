#!/usr/bin/env python3
"""Prepare or run a PixelRAG visual index build for the EY corpus.

PixelRAG is pixel-native: it renders documents to screenshots/tiles, embeds
those image tiles with a vision-language embedding model, and builds a FAISS
index. This complements the existing text/Qdrant chunk indexes instead of
replacing them.

Default source: data/
Default output: pixelrag_indexes/ey_visual_index/

Install dependency when you are ready to build:
    pip install "pixelrag[index]"
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


PIXELRAG_LOCAL_EXTENSIONS = {".pdf", ".html", ".htm", ".png", ".jpg", ".jpeg"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a PixelRAG visual-index config and optionally build it."
    )
    parser.add_argument(
        "--source",
        default="data",
        help="Source PDF/image/text folder or file for PixelRAG local source.",
    )
    parser.add_argument(
        "--output",
        default="pixelrag_indexes/ey_visual_index",
        help="PixelRAG output index directory.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Config path. Defaults to <output>/pixelrag.yaml.",
    )
    parser.add_argument(
        "--model",
        default="Qwen/Qwen3-VL-Embedding-2B",
        help="PixelRAG visual embedding model.",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cpu", "cuda", "mps"],
        help="Embedding device written to pixelrag.yaml.",
    )
    parser.add_argument(
        "--tile-height",
        type=int,
        default=8192,
        help="PixelRAG render tile height.",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=85,
        help="JPEG quality for rendered tiles.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of source documents to build. Useful for smoke tests.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ask PixelRAG to clean/rebuild intermediate tiles and embeddings.",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Run `pixelrag index build` after writing the config.",
    )
    parser.add_argument(
        "--pixelrag-command",
        default="pixelrag",
        help="PixelRAG CLI command or absolute executable path.",
    )
    return parser.parse_args()


def yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def yaml_path(path: Path) -> str:
    return path.resolve().as_posix()


def source_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source] if source.suffix.lower() in PIXELRAG_LOCAL_EXTENSIONS else []
    if source.is_dir():
        return [
            path
            for path in sorted(source.rglob("*"))
            if path.is_file() and path.suffix.lower() in PIXELRAG_LOCAL_EXTENSIONS
        ]
    return []


def stage_pixelrag_source(source: Path, output_dir: Path) -> tuple[Path, list[dict[str, str]]]:
    """Stage files with numeric stems because PixelRAG's pipeline expects int IDs."""
    files = source_files(source)
    staging_dir = output_dir / "_source"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    mapping: list[dict[str, str]] = []
    for index, file_path in enumerate(files):
        staged_name = f"{index:06d}{file_path.suffix.lower()}"
        staged_path = staging_dir / staged_name
        shutil.copy2(file_path, staged_path)
        mapping.append(
            {
                "pixelrag_id": f"{index:06d}",
                "staged_name": staged_name,
                "staged_path": yaml_path(staged_path),
                "original_name": file_path.name,
                "original_path": yaml_path(file_path),
            }
        )

    mapping_path = output_dir / "_source_mapping.json"
    mapping_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    return staging_dir, mapping


def write_config(args: argparse.Namespace) -> Path:
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path = (
        Path(args.config).resolve()
        if args.config
        else output_dir / "pixelrag.yaml"
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    source_path = Path(args.source).resolve()
    staged_source, mapping = stage_pixelrag_source(source_path, output_dir)
    if not mapping:
        raise SystemExit(
            f"No PixelRAG-supported files found in {source_path}. "
            f"Supported extensions: {', '.join(sorted(PIXELRAG_LOCAL_EXTENSIONS))}"
        )

    config = "\n".join(
        [
            "source:",
            "  type: local",
            f"  path: {yaml_quote(yaml_path(staged_source))}",
            "",
            "ingest:",
            "  backend: cdp",
            f"  quality: {args.quality}",
            f"  tile_height: {args.tile_height}",
            "",
            "embed:",
            f"  model: {yaml_quote(args.model)}",
            f"  device: {args.device}",
            "",
            f"output: {yaml_quote(yaml_path(output_dir))}",
            "",
        ]
    )
    config_path.write_text(config, encoding="utf-8")
    return config_path


def count_source_files(source: Path) -> int:
    return len(source_files(source))


def check_pixelrag(command: str) -> str | None:
    if Path(command).exists():
        return command
    return shutil.which(command)


def require_pixelrag_package() -> None:
    if sys.version_info < (3, 12):
        raise SystemExit(
            "PixelRAG declares Python >= 3.12.\n"
            f"Current interpreter: {sys.version.split()[0]}\n"
            "Run this with the project Python 3.12 venv."
        )
    if importlib.util.find_spec("pixelrag") is None:
        raise SystemExit(
            "PixelRAG is not installed in this Python environment.\n"
            "Install it first:\n"
            '  venv\\Scripts\\python.exe -m pip install "pixelrag[index]"\n'
            "Then rerun this script with --build."
        )


def pixelrag_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    runtime_bin = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "bin"
    )
    if runtime_bin.exists():
        env["PATH"] = f"{runtime_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def build_index(args: argparse.Namespace, config_path: Path) -> None:
    if args.pixelrag_command == "pixelrag":
        require_pixelrag_package()
        cmd = [
            sys.executable,
            "-m",
            "pixelrag.cli",
            "index",
            "build",
            "--config",
            str(config_path),
        ]
    else:
        command = check_pixelrag(args.pixelrag_command)
        if command is None:
            raise SystemExit(f"PixelRAG command was not found: {args.pixelrag_command}")
        cmd = [command, "index", "build", "--config", str(config_path)]

    if args.limit is not None:
        cmd.extend(["--limit", str(args.limit)])
    if args.force:
        cmd.append("--force")

    print("Running:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, env=pixelrag_subprocess_env())


def main() -> None:
    args = parse_args()
    source = Path(args.source)
    if not source.exists():
        raise SystemExit(f"Source path does not exist: {source}")

    config_path = write_config(args)
    source_count = count_source_files(source)
    print(f"Wrote PixelRAG config: {config_path}")
    print(f"Source files detected: {source_count}")
    print(f"Output directory: {Path(args.output)}")

    if not args.build:
        print("")
        print("Build command:")
        print(f"  {sys.executable} {Path(__file__)} --build")
        print("")
        print("Or call PixelRAG directly:")
        print(f"  pixelrag index build --config {config_path}")
        return

    build_index(args, config_path)


if __name__ == "__main__":
    main()
