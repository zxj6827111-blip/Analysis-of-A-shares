"""Download legally approved metaphysics corpus sources from the source manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


USER_AGENT = "AStockMetaphysicsCorpusCollector/1.0 (local research corpus)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
CHUNK_SIZE = 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download only sources explicitly approved for automated collection."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("docs/命理知识库书目来源清单.json"),
        help="Path to the machine-readable source manifest.",
    )
    parser.add_argument(
        "--storage",
        type=Path,
        default=Path("storage/astock"),
        help="AStock storage root. Files are written below metaphysics/corpus/originals.",
    )
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        help="Source ID to download. Repeat to select multiple sources.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print eligible documents without downloading them.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing file after validating a new copy.",
    )
    return parser.parse_args()


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("schema_version") != "1.0":
        raise ValueError(f"Unsupported manifest schema: {manifest.get('schema_version')!r}")
    if not isinstance(manifest.get("sources"), list):
        raise ValueError("Manifest must contain a sources list")
    return manifest


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def resolve_commons_document(document: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    remote_id = str(document["remote_id"])
    query = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "prop": "imageinfo",
            "iiprop": "url|size|sha1|mime|extmetadata",
            "pageids": remote_id,
        }
    )
    payload = request_json(f"{COMMONS_API}?{query}")
    page = payload.get("query", {}).get("pages", {}).get(remote_id)
    if not page or not page.get("imageinfo"):
        raise ValueError(f"Commons page {remote_id} has no downloadable image")

    image_info = page["imageinfo"][0]
    metadata = image_info.get("extmetadata", {})
    license_name = metadata.get("LicenseShortName", {}).get("value", "")
    copyrighted = metadata.get("Copyrighted", {}).get("value", "")
    if license_name != "Public domain" or copyrighted not in ("False", False, ""):
        raise PermissionError(
            f"Commons page {remote_id} is not currently marked Public domain"
        )

    return image_info["url"], {
        "provider_title": page.get("title"),
        "provider_license": license_name,
        "provider_mime": image_info.get("mime"),
        "provider_size_bytes": image_info.get("size"),
        "provider_sha1": image_info.get("sha1"),
        "provider_description_url": image_info.get("descriptionurl"),
    }


def resolve_mediawiki_snapshot(document: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "action": "parse",
            "format": "json",
            "formatversion": "2",
            "page": document["page"],
            "prop": "text|wikitext|revid|displaytitle|properties",
        }
    )
    return f"{document['api_endpoint']}?{query}", {
        "provider_title": document["page"],
        "snapshot_type": "mediawiki_parse_api",
    }


def resolve_document(document: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    download_type = document.get("download_type")
    if download_type == "wikimedia_commons_pageid":
        return resolve_commons_document(document)
    if download_type == "mediawiki_page_snapshot":
        return resolve_mediawiki_snapshot(document)
    if download_type == "direct_url":
        return document["url"], {"snapshot_type": "direct_url"}
    raise ValueError(f"Unsupported download_type: {download_type!r}")


def hash_existing(path: Path) -> dict[str, Any]:
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            size += len(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
    return {
        "size_bytes": size,
        "sha1": sha1.hexdigest(),
        "sha256": sha256.hexdigest(),
    }


def validate_hashes(
    document: dict[str, Any],
    actual: dict[str, Any],
    provider: dict[str, Any],
) -> None:
    expected_size = document.get("expected_size_bytes")
    expected_sha1 = document.get("expected_sha1")
    provider_size = provider.get("provider_size_bytes")
    provider_sha1 = provider.get("provider_sha1")

    if expected_size is not None and actual["size_bytes"] != expected_size:
        raise ValueError(
            f"Size mismatch: expected {expected_size}, got {actual['size_bytes']}"
        )
    if provider_size is not None and actual["size_bytes"] != provider_size:
        raise ValueError(
            f"Provider size mismatch: expected {provider_size}, got {actual['size_bytes']}"
        )
    if expected_sha1 and actual["sha1"].lower() != expected_sha1.lower():
        raise ValueError(
            f"SHA-1 mismatch: expected {expected_sha1}, got {actual['sha1']}"
        )
    if provider_sha1 and actual["sha1"].lower() != provider_sha1.lower():
        raise ValueError(
            f"Provider SHA-1 mismatch: expected {provider_sha1}, got {actual['sha1']}"
        )


def download_to_path(url: str, target: Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    size = 0

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".part",
        dir=target.parent,
    )
    try:
        with os.fdopen(fd, "wb") as output:
            with urllib.request.urlopen(request, timeout=120) as response:
                while chunk := response.read(CHUNK_SIZE):
                    output.write(chunk)
                    size += len(chunk)
                    sha1.update(chunk)
                    sha256.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        return {
            "temp_path": Path(temp_name),
            "size_bytes": size,
            "sha1": sha1.hexdigest(),
            "sha256": sha256.hexdigest(),
        }
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def select_sources(
    manifest: dict[str, Any], requested_ids: list[str] | None
) -> list[dict[str, Any]]:
    sources = manifest["sources"]
    if not requested_ids:
        return sources

    by_id = {source["source_id"]: source for source in sources}
    missing = sorted(set(requested_ids) - set(by_id))
    if missing:
        raise ValueError(f"Unknown source IDs: {', '.join(missing)}")
    return [by_id[source_id] for source_id in requested_ids]


def receipt_path(storage: Path) -> Path:
    return storage / "metaphysics" / "manifests" / "download_receipt.json"


def write_receipt(storage: Path, entries: list[dict[str, Any]]) -> None:
    path = receipt_path(storage)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    temp = path.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temp.replace(path)


def run(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    selected = select_sources(manifest, args.sources)
    originals_root = args.storage / manifest["default_storage_subdir"]
    entries: list[dict[str, Any]] = []
    failures = 0

    for source in selected:
        source_id = source["source_id"]
        if not source.get("automated_download_allowed", False):
            print(f"SKIP restricted/manual source: {source_id}")
            continue

        for document in source.get("documents", []):
            document_id = document["document_id"]
            target = originals_root / source_id / document["filename"]
            if args.dry_run:
                print(f"WOULD DOWNLOAD {source_id}/{document_id} -> {target}")
                continue

            entry: dict[str, Any] = {
                "source_id": source_id,
                "edition_id": source["edition_id"],
                "document_id": document_id,
                "target_path": str(target),
            }
            try:
                url, provider = resolve_document(document)
                entry["resolved_url"] = url
                entry["provider"] = provider

                if target.exists() and not args.overwrite:
                    actual = hash_existing(target)
                    validate_hashes(document, actual, provider)
                    entry.update(actual)
                    entry["status"] = "existing_valid"
                    print(f"VALID existing: {target}")
                else:
                    actual = download_to_path(url, target)
                    temp_path = actual.pop("temp_path")
                    try:
                        validate_hashes(document, actual, provider)
                        if target.exists() and args.overwrite:
                            target.unlink()
                        temp_path.replace(target)
                    except Exception:
                        temp_path.unlink(missing_ok=True)
                        raise
                    entry.update(actual)
                    entry["status"] = "downloaded"
                    print(
                        f"DOWNLOADED {target} "
                        f"({actual['size_bytes']} bytes, sha256={actual['sha256']})"
                    )
            except Exception as exc:
                failures += 1
                entry["status"] = "failed"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                print(
                    f"FAILED {source_id}/{document_id}: {entry['error']}",
                    file=sys.stderr,
                )
            entries.append(entry)

    if not args.dry_run:
        write_receipt(args.storage, entries)
        print(f"Receipt: {receipt_path(args.storage)}")
    return 1 if failures else 0


def main() -> int:
    try:
        return run(parse_args())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
