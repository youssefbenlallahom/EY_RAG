#!/usr/bin/env python3
"""Export and restore Qdrant collection snapshots.

Use this to copy already-ingested Qdrant collections to another machine without
recomputing embeddings or re-running ingestion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor
except ImportError:  # pragma: no cover - optional runtime fallback
    MultipartEncoder = None
    MultipartEncoderMonitor = None


TEXT_COLLECTIONS = [
    "ey_rag_langchain_markdown_recursive",
    "ey_rag_llamaindex_semantic",
    "ey_rag_haystack_document_splitter",
    "ey_rag_chonkie_semantic",
]

MB = 1024 * 1024


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


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description=(
            "Create or restore a portable Qdrant snapshot bundle. "
            "Snapshots copy vectors and payloads directly, so no ingestion is needed on the target PC."
        )
    )
    parser.add_argument("--qdrant-url", default=env_value("QDRANT_URL", default="http://localhost:6333"))
    parser.add_argument("--api-key", default=env_value("QDRANT_API_KEY"), help="Optional Qdrant API key.")
    parser.add_argument("--timeout", type=int, default=300)

    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="Export local Qdrant collections into a .zip bundle.")
    export.add_argument(
        "--collections",
        default="text",
        help=(
            "'text' for the four text RAG collections, 'all' for every ey_rag* collection, "
            "or a comma-separated collection list."
        ),
    )
    export.add_argument(
        "--output",
        default="qdrant_transfer/ey_rag_qdrant_snapshots.zip",
        help="Destination .zip bundle.",
    )
    export.add_argument(
        "--prefix",
        default="ey_rag",
        help="Prefix used when --collections all is selected.",
    )
    export.add_argument(
        "--delete-server-snapshots-after-download",
        action="store_true",
        help="Delete Qdrant-side snapshot files after they have been downloaded into the bundle.",
    )

    restore = subparsers.add_parser("restore", help="Restore collections from a .zip snapshot bundle.")
    restore.add_argument(
        "--bundle",
        default="qdrant_transfer/ey_rag_qdrant_snapshots.zip",
        help="Snapshot .zip bundle created by the export command.",
    )
    restore.add_argument(
        "--collections",
        default="all",
        help="'all' for every collection in the bundle, or a comma-separated subset.",
    )
    restore.add_argument(
        "--yes",
        action="store_true",
        help="Skip overwrite confirmation. Restore will overwrite target collections with snapshot data.",
    )
    restore.add_argument(
        "--skip-checksum",
        action="store_true",
        help="Do not pass snapshot checksums to Qdrant during upload restore.",
    )
    restore.add_argument(
        "--cloud",
        action="store_true",
        help="Restore to Qdrant Cloud using QDRANT_CLOUD_URL and QDRANT_CLOUD_API_KEY from .env.",
    )
    restore.add_argument(
        "--upload-progress-mb",
        type=int,
        default=16,
        help="Print upload progress after this many MB. Use 0 to disable progress logs.",
    )
    restore.add_argument(
        "--upload-retries",
        type=int,
        default=2,
        help="Retry failed snapshot uploads this many times.",
    )
    restore.add_argument(
        "--read-timeout-multiplier",
        type=float,
        default=1.0,
        help="Multiply the base timeout by this factor for read operations during upload. Use 2.0 or higher for cloud uploads.",
    )

    inspect = subparsers.add_parser("inspect", help="Print the manifest inside a snapshot bundle.")
    inspect.add_argument(
        "--bundle",
        default="qdrant_transfer/ey_rag_qdrant_snapshots.zip",
        help="Snapshot .zip bundle created by the export command.",
    )
    collections = subparsers.add_parser("collections", help="List collections in a target Qdrant instance.")
    collections.add_argument(
        "--cloud",
        action="store_true",
        help="List Qdrant Cloud collections using QDRANT_CLOUD_URL and QDRANT_CLOUD_API_KEY from .env.",
    )
    collections.add_argument(
        "--prefix",
        default="ey_rag",
        help="Only show collections with this prefix. Use an empty value to show every collection.",
    )
    args = parser.parse_args()
    if args.command in {"restore", "collections"} and args.cloud:
        args.qdrant_url = env_value("QDRANT_CLOUD_URL")
        args.api_key = env_value("QDRANT_CLOUD_API_KEY")
        if not args.qdrant_url:
            raise SystemExit("QDRANT_CLOUD_URL is missing from .env.")
        if not args.api_key:
            raise SystemExit("QDRANT_CLOUD_API_KEY is missing from .env.")
    return args


def headers(api_key: str | None) -> dict[str, str]:
    return {"api-key": api_key} if api_key else {}


def qdrant_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def request_json(
    method: str,
    base_url: str,
    path: str,
    api_key: str | None,
    timeout: int,
    **kwargs: Any,
) -> dict[str, Any]:
    response = requests.request(
        method,
        qdrant_url(base_url, path),
        headers=headers(api_key),
        timeout=timeout,
        **kwargs,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Qdrant request failed: {method} {path} -> {response.status_code}\n"
            f"{response.text[:1000]}"
        )
    return response.json() if response.content else {}


def check_qdrant(base_url: str, api_key: str | None, timeout: int) -> dict[str, Any]:
    try:
        return request_json("GET", base_url, "/", api_key, timeout)
    except Exception as exc:
        raise SystemExit(
            f"Qdrant is not reachable at {base_url}.\n"
            "Start it first, for example:\n"
            "  docker compose up -d qdrant\n"
            f"Original error: {type(exc).__name__}: {exc}"
        ) from exc


def list_collections(base_url: str, api_key: str | None, timeout: int) -> list[str]:
    data = request_json("GET", base_url, "/collections", api_key, timeout)
    collections = data.get("result", {}).get("collections", [])
    return sorted(str(item.get("name")) for item in collections if item.get("name"))


def collection_info(base_url: str, collection: str, api_key: str | None, timeout: int) -> dict[str, Any]:
    data = request_json("GET", base_url, f"/collections/{collection}", api_key, timeout)
    return data.get("result", {})


def resolve_export_collections(args: argparse.Namespace) -> list[str]:
    requested = str(args.collections).strip()
    if requested.lower() == "text":
        return list(TEXT_COLLECTIONS)
    existing = list_collections(args.qdrant_url, args.api_key, args.timeout)
    if requested.lower() == "all":
        return [name for name in existing if name.startswith(args.prefix)]
    return [item.strip() for item in requested.split(",") if item.strip()]


def resolve_restore_collections(manifest: dict[str, Any], value: str) -> list[dict[str, Any]]:
    snapshots = manifest.get("snapshots") or []
    if value.strip().lower() == "all":
        return snapshots
    requested = {item.strip() for item in value.split(",") if item.strip()}
    selected = [item for item in snapshots if item.get("collection") in requested]
    missing = sorted(requested - {str(item.get("collection")) for item in selected})
    if missing:
        raise SystemExit(f"Collections not found in bundle manifest: {', '.join(missing)}")
    return selected


def create_snapshot(
    base_url: str,
    collection: str,
    api_key: str | None,
    timeout: int,
) -> dict[str, Any]:
    data = request_json(
        "POST",
        base_url,
        f"/collections/{collection}/snapshots",
        api_key,
        timeout,
        params={"wait": "true"},
    )
    snapshot = data.get("result") or {}
    if not snapshot.get("name"):
        raise RuntimeError(f"Snapshot creation returned no name for collection {collection}: {data}")
    return snapshot


def download_snapshot(
    base_url: str,
    collection: str,
    snapshot_name: str,
    destination: Path,
    api_key: str | None,
    timeout: int,
) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    sha256 = hashlib.sha256()
    with requests.get(
        qdrant_url(base_url, f"/collections/{collection}/snapshots/{snapshot_name}"),
        headers=headers(api_key),
        stream=True,
        timeout=timeout,
    ) as response:
        if response.status_code >= 400:
            raise RuntimeError(
                f"Snapshot download failed for {collection}/{snapshot_name}: "
                f"{response.status_code}\n{response.text[:1000]}"
            )
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                sha256.update(chunk)
                handle.write(chunk)
    return sha256.hexdigest()


def delete_server_snapshot(
    base_url: str,
    collection: str,
    snapshot_name: str,
    api_key: str | None,
    timeout: int,
) -> None:
    request_json(
        "DELETE",
        base_url,
        f"/collections/{collection}/snapshots/{snapshot_name}",
        api_key,
        timeout,
        params={"wait": "true"},
    )


def write_zip(source_dir: Path, output_zip: Path) -> None:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    if output_zip.exists():
        output_zip.unlink()
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source_dir).as_posix())


def read_manifest_from_bundle(bundle: Path) -> dict[str, Any]:
    if not bundle.exists():
        raise SystemExit(f"Bundle does not exist: {bundle}")
    with zipfile.ZipFile(bundle, "r") as archive:
        with archive.open("manifest.json") as handle:
            return json.loads(handle.read().decode("utf-8"))


def export_snapshots(args: argparse.Namespace) -> None:
    qdrant_info = check_qdrant(args.qdrant_url, args.api_key, args.timeout)
    collections = resolve_export_collections(args)
    if not collections:
        raise SystemExit("No collections selected for export.")

    existing = set(list_collections(args.qdrant_url, args.api_key, args.timeout))
    missing = [name for name in collections if name not in existing]
    if missing:
        raise SystemExit(f"Selected collections are missing in Qdrant: {', '.join(missing)}")

    output_zip = Path(args.output)
    with tempfile.TemporaryDirectory(prefix="qdrant_snapshot_export_") as temp_name:
        staging = Path(temp_name)
        manifest: dict[str, Any] = {
            "format": "ey_rag_qdrant_snapshot_bundle",
            "version": 1,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_qdrant_url": args.qdrant_url,
            "source_qdrant_info": qdrant_info,
            "collections": collections,
            "snapshots": [],
        }

        for index, collection in enumerate(collections, start=1):
            print(f"[{index}/{len(collections)}] Creating snapshot for {collection} ...", flush=True)
            info = collection_info(args.qdrant_url, collection, args.api_key, args.timeout)
            snapshot = create_snapshot(args.qdrant_url, collection, args.api_key, args.timeout)
            snapshot_name = str(snapshot["name"])
            relative_path = Path("snapshots") / collection / snapshot_name
            destination = staging / relative_path

            print(f"    Downloading {snapshot_name} ...", flush=True)
            local_sha256 = download_snapshot(
                args.qdrant_url,
                collection,
                snapshot_name,
                destination,
                args.api_key,
                args.timeout,
            )
            size_bytes = destination.stat().st_size
            manifest["snapshots"].append(
                {
                    "collection": collection,
                    "snapshot_name": snapshot_name,
                    "relative_path": relative_path.as_posix(),
                    "size_bytes": size_bytes,
                    "qdrant_checksum": snapshot.get("checksum"),
                    "local_sha256": local_sha256,
                    "vectors_count": info.get("vectors_count"),
                    "points_count": info.get("points_count"),
                    "indexed_vectors_count": info.get("indexed_vectors_count"),
                }
            )
            print(f"    Saved {size_bytes / (1024 * 1024):.1f} MB", flush=True)

            if args.delete_server_snapshots_after_download:
                print(f"    Deleting server snapshot {snapshot_name} ...", flush=True)
                delete_server_snapshot(args.qdrant_url, collection, snapshot_name, args.api_key, args.timeout)

        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (staging / "RESTORE_COMMANDS.md").write_text(restore_commands_doc(output_zip.name), encoding="utf-8")
        write_zip(staging, output_zip)

    print(f"\nExport complete: {output_zip.resolve()}")
    print("Send this .zip file to your friend.")


def restore_commands_doc(bundle_name: str) -> str:
    return f"""# Restore Qdrant Snapshot Bundle

Start Qdrant first:

```powershell
docker compose up -d qdrant
```

Inspect the bundle:

```powershell
python scripts\\indexing\\transfer_qdrant_snapshots.py inspect --bundle {bundle_name}
```

Restore all collections:

```powershell
python scripts\\indexing\\transfer_qdrant_snapshots.py restore --bundle {bundle_name} --yes
```

This overwrites target collections with the snapshot data.
"""


def inspect_bundle(args: argparse.Namespace) -> None:
    manifest = read_manifest_from_bundle(Path(args.bundle))
    print(json.dumps(manifest, indent=2))


def extract_bundle(bundle: Path, target_dir: Path) -> dict[str, Any]:
    with zipfile.ZipFile(bundle, "r") as archive:
        archive.extractall(target_dir)
    manifest_path = target_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"Bundle has no manifest.json: {bundle}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def confirm_restore(selected: list[dict[str, Any]], qdrant_url_value: str) -> None:
    names = ", ".join(str(item["collection"]) for item in selected)
    print("Restore will overwrite/create these Qdrant collections:")
    print(f"  {names}")
    print(f"Target Qdrant: {qdrant_url_value}")
    answer = input("Type RESTORE to continue: ").strip()
    if answer != "RESTORE":
        raise SystemExit("Restore cancelled.")


def upload_snapshot(
    base_url: str,
    collection: str,
    snapshot_path: Path,
    checksum: str | None,
    api_key: str | None,
    timeout: int,
    skip_checksum: bool,
    progress_mb: int,
    retries: int,
    read_timeout_multiplier: float = 1.0,
) -> dict[str, Any]:
    params = {"wait": "true", "priority": "snapshot"}
    if checksum and not skip_checksum:
        params["checksum"] = checksum

    last_error: Exception | None = None
    max_attempts = max(retries, 0) + 1
    for attempt in range(1, max_attempts + 1):
        try:
            response = upload_snapshot_once(
                base_url,
                collection,
                snapshot_path,
                params,
                api_key,
                timeout,
                progress_mb,
                attempt,
                max_attempts,
                read_timeout_multiplier,
            )
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= max_attempts:
                raise
            # Use longer delays for SSL errors, which often indicate network instability
            is_ssl_error = "SSL" in str(type(exc).__name__) or "SSL" in str(exc)
            base_delay = 10 if is_ssl_error else 5
            delay = min(base_delay * attempt, 60 if is_ssl_error else 30)
            print(
                f"    Upload attempt {attempt}/{max_attempts} failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
            print(f"    Retrying in {delay}s ...", flush=True)
            time.sleep(delay)
    else:
        raise RuntimeError(f"Snapshot upload failed: {last_error}") from last_error

    if response.status_code >= 400:
        raise RuntimeError(
            f"Snapshot restore failed for {collection}: {response.status_code}\n"
            f"{response.text[:1000]}"
        )
    return response.json() if response.content else {}


def upload_snapshot_once(
    base_url: str,
    collection: str,
    snapshot_path: Path,
    params: dict[str, str],
    api_key: str | None,
    timeout: int,
    progress_mb: int,
    attempt: int,
    max_attempts: int,
    read_timeout_multiplier: float = 1.0,
) -> requests.Response:
    # Use separate connect and read timeouts
    # Connect timeout: 60s (reasonable for establishing connection)
    # Read timeout: base timeout * multiplier (for large file transfers)
    read_timeout = int(timeout * read_timeout_multiplier)
    timeout_tuple = (60, read_timeout)
    
    if MultipartEncoder is None or MultipartEncoderMonitor is None:
        print(
            "    requests-toolbelt is not installed; using standard requests upload without streaming progress.",
            flush=True,
        )
        with snapshot_path.open("rb") as handle:
            return requests.post(
                qdrant_url(base_url, f"/collections/{collection}/snapshots/upload"),
                headers=headers(api_key),
                params=params,
                files={"snapshot": (snapshot_path.name, handle, "application/octet-stream")},
                timeout=timeout_tuple,
            )

    file_size = snapshot_path.stat().st_size
    progress_bytes = max(progress_mb, 0) * MB
    state = {
        "last_report": 0,
        "started": time.time(),
        "sent_notice": False,
    }

    with snapshot_path.open("rb") as handle:
        encoder = MultipartEncoder(
            fields={"snapshot": (snapshot_path.name, handle, "application/octet-stream")}
        )
        total_bytes = encoder.len

        def callback(monitor: MultipartEncoderMonitor) -> None:
            if progress_bytes and monitor.bytes_read - state["last_report"] >= progress_bytes:
                elapsed = max(time.time() - state["started"], 0.001)
                percent = monitor.bytes_read / total_bytes * 100 if total_bytes else 100
                speed = monitor.bytes_read / MB / elapsed
                print(
                    f"    Uploaded {monitor.bytes_read / MB:.1f}/{total_bytes / MB:.1f} MB "
                    f"({percent:.1f}%, {speed:.1f} MB/s)",
                    flush=True,
                )
                state["last_report"] = monitor.bytes_read
            if monitor.bytes_read >= total_bytes and not state["sent_notice"]:
                state["sent_notice"] = True
                print("    Upload sent; waiting for Qdrant to apply the snapshot ...", flush=True)

        monitor = MultipartEncoderMonitor(encoder, callback)
        request_headers = headers(api_key)
        request_headers["Content-Type"] = monitor.content_type
        if max_attempts > 1:
            print(f"    Upload attempt {attempt}/{max_attempts}", flush=True)
        print(f"    Multipart payload: {total_bytes / MB:.1f} MB; snapshot file: {file_size / MB:.1f} MB", flush=True)
        if read_timeout_multiplier != 1.0:
            print(f"    Using read timeout: {read_timeout}s (base: {timeout}s × {read_timeout_multiplier})", flush=True)

        return requests.post(
            qdrant_url(base_url, f"/collections/{collection}/snapshots/upload"),
            headers=request_headers,
            params=params,
            data=monitor,
            timeout=timeout_tuple,
        )


def restore_snapshots(args: argparse.Namespace) -> None:
    check_qdrant(args.qdrant_url, args.api_key, args.timeout)
    bundle = Path(args.bundle)
    with tempfile.TemporaryDirectory(prefix="qdrant_snapshot_restore_") as temp_name:
        extracted = Path(temp_name)
        manifest = extract_bundle(bundle, extracted)
        selected = resolve_restore_collections(manifest, args.collections)
        if not selected:
            raise SystemExit("No snapshots selected for restore.")
        if not args.yes:
            confirm_restore(selected, args.qdrant_url)

        for index, item in enumerate(selected, start=1):
            collection = str(item["collection"])
            snapshot_path = extracted / str(item["relative_path"])
            if not snapshot_path.exists():
                raise SystemExit(f"Snapshot file is missing from bundle: {snapshot_path}")
            checksum = item.get("qdrant_checksum") or item.get("local_sha256")
            print(f"[{index}/{len(selected)}] Restoring {collection} from {snapshot_path.name} ...", flush=True)
            print(f"    Snapshot size after extraction: {snapshot_path.stat().st_size / MB:.1f} MB", flush=True)
            upload_snapshot(
                args.qdrant_url,
                collection,
                snapshot_path,
                str(checksum) if checksum else None,
                args.api_key,
                args.timeout,
                args.skip_checksum,
                args.upload_progress_mb,
                args.upload_retries,
                args.read_timeout_multiplier,
            )
            info = collection_info(args.qdrant_url, collection, args.api_key, args.timeout)
            print(
                "    Restored. "
                f"points={info.get('points_count')} vectors={info.get('vectors_count')}",
                flush=True,
            )

    print("\nRestore complete.")


def print_collections(args: argparse.Namespace) -> None:
    check_qdrant(args.qdrant_url, args.api_key, args.timeout)
    collection_names = list_collections(args.qdrant_url, args.api_key, args.timeout)
    if args.prefix:
        collection_names = [name for name in collection_names if name.startswith(args.prefix)]
    if not collection_names:
        print("No matching collections found.")
        return

    for name in collection_names:
        info = collection_info(args.qdrant_url, name, args.api_key, args.timeout)
        print(
            f"{name}: status={info.get('status')} "
            f"points={info.get('points_count')} vectors={info.get('vectors_count')}"
        )


def main() -> None:
    args = parse_args()
    started = time.time()
    if args.command == "export":
        export_snapshots(args)
    elif args.command == "restore":
        restore_snapshots(args)
    elif args.command == "inspect":
        inspect_bundle(args)
    elif args.command == "collections":
        print_collections(args)
    else:
        raise SystemExit(f"Unknown command: {args.command}")
    print(f"Elapsed: {round(time.time() - started, 1)}s")


if __name__ == "__main__":
    main()
