"""Transactional orchestration for the deterministic PDF import slice."""

from __future__ import annotations

import io
import json
import math
import tempfile
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import PaperInventory, PaperSource, PdfBlock, PdfRelation
from .pdf_backend import PdfBackendRun
from .pdf_backend_pdfplumber import PdfPlumberBackend
from .pdf_contracts import (
    PdfUnsupportedError,
    load_policy,
    make_run_id,
    sha256_bytes,
    stable_id,
)
from .pdf_layout import recover_layout
from .pdf_markdown import materialize_markdown
from .pdf_qa import build_qa, render_qa_markdown
from .storage import atomic_write_bytes, atomic_write_json, atomic_write_text, canonical_json


def import_pdf(
    root: Path,
    source: Path,
    data: bytes,
    *,
    policy_path: Path | None = None,
) -> PaperSource:
    if len(data) < 5 or not data.startswith(b"%PDF-"):
        raise ValueError("PDF_CORRUPT: input does not begin with a PDF header")
    policy, policy_sha256 = load_policy(policy_path)
    if len(data) > policy["max_file_bytes"]:
        raise PdfUnsupportedError(
            f"PDF_RESOURCE_LIMIT: {len(data)} bytes exceeds {policy['max_file_bytes']}"
        )
    source_sha256 = sha256_bytes(data)
    backend = PdfPlumberBackend()

    with tempfile.TemporaryDirectory(prefix=".paperweaver-pdf-", dir=root) as directory:
        stage = Path(directory) / "source"
        staged_original = stage / "original.pdf"
        atomic_write_bytes(staged_original, data)
        try:
            run = backend.extract(staged_original, policy)
        except (PdfUnsupportedError, RuntimeError, ValueError):
            raise
        except Exception as error:
            raise ValueError(f"PDF_CORRUPT: unable to parse PDF: {error}") from error
        run_id = make_run_id(
            source_sha256, run.name, run.version, run.options, policy_sha256
        )
        layout = recover_layout(run, source_sha256, run_id, policy)
        try:
            blocks, assets, asset_paths, visible_ink_ratio = _attach_visual_evidence(
                staged_original, stage, layout.blocks, run, policy
            )
        except (PdfUnsupportedError, RuntimeError, ValueError):
            raise
        except Exception as error:
            raise ValueError(f"PDF_RENDER_FAILED: unable to render PDF evidence: {error}") from error
        metrics = {
            **layout.metrics,
            "visible_ink_component_accounting_ratio": visible_ink_ratio,
        }
        materialization_id = stable_id(
            "mat", run_id, sha256_bytes(canonical_json([item.to_dict() for item in blocks]).encode())
        )
        markdown, article_map, render_tree = materialize_markdown(
            blocks, materialization_id, asset_paths
        )
        qa = build_qa(
            source_sha256,
            run_id,
            materialization_id,
            policy,
            policy_sha256,
            metrics,
            blocks,
        )
        title = next(
            (item.text for item in blocks if item.kind == "document_title" and item.text),
            "",
        )
        record = PaperSource(
            title,
            "source/article.md",
            source_sha256,
            "pdf",
            "source/original.pdf",
        )
        inventory = PaperInventory(
            "pdf",
            qa["metrics"]["figures"],
            qa["metrics"]["tables"],
            qa["metrics"]["equations"],
            0,
            sum(item.kind == "reference" for item in blocks),
            [item["code"] for item in qa["issues"]],
        )
        manifest = {
            "schema_version": 1,
            "source_sha256": source_sha256,
            "active_run_id": run_id,
            "materialization_id": materialization_id,
            "article_sha256": sha256_bytes(markdown.encode()),
            "policy": {"name": policy["name"], "sha256": policy_sha256},
            "backend": {"name": run.name, "version": run.version, "options": run.options},
            "status": qa["status"],
            "frozen_at": None,
        }
        _write_stage(
            stage,
            run,
            run_id,
            policy,
            blocks,
            layout.accounting,
            markdown,
            article_map,
            render_tree,
            qa,
            assets,
            record,
            inventory,
            manifest,
        )
        _commit_stage(root / "source", stage, source_sha256)
    return record


def _attach_visual_evidence(
    source: Path,
    stage: Path,
    blocks: list[PdfBlock],
    run: PdfBackendRun,
    policy: dict[str, Any],
) -> tuple[list[PdfBlock], list[dict[str, Any]], dict[str, str], float]:
    try:
        import pypdfium2 as pdfium
    except ImportError as error:
        raise RuntimeError(
            "PDF_BACKEND_MISSING: Install paperweaver[pdf] to render PDF evidence."
        ) from error
    document = pdfium.PdfDocument(str(source))
    scale = float(policy["render_scale"])
    assets: list[dict[str, Any]] = []
    asset_paths: dict[str, str] = {}
    assets_by_block: dict[str, str] = {}
    asset_ids_by_digest: dict[str, str] = {}
    accounted_ink = total_ink = 0
    for index, page in enumerate(run.pages):
        pixels = round(page.width * scale) * round(page.height * scale)
        if pixels > policy["max_rendered_page_pixels"]:
            raise PdfUnsupportedError(
                f"PDF_RESOURCE_LIMIT: page {page.page} render has {pixels} pixels; "
                f"limit is {policy['max_rendered_page_pixels']}"
            )
        page_image = document[index].render(scale=scale).to_pil().convert("RGB")
        page_blocks = [
            block
            for block in blocks
            if any(item["page"] == page.page for item in block.provenance)
        ]
        page_accounted, page_total = _page_ink_counts(
            page.objects, page_image, scale, policy
        )
        accounted_ink += page_accounted
        total_ink += page_total
        for block in page_blocks:
            if block.status != "unresolved" and block.kind not in {
                "figure",
                "table",
                "equation",
            }:
                continue
            provenance = block.provenance[0]
            x0, y0, x1, y1 = provenance["bbox"]
            margin = 0.0 if block.kind == "unknown" else (3.0 if block.kind == "equation" else 8.0)
            bbox = [
                max(0.0, x0 - margin),
                max(0.0, y0 - margin),
                min(page.width, x1 + margin),
                min(page.height, y1 + margin),
            ]
            crop = page_image.crop(
                _raster_crop_box(bbox, scale, (page_image.width, page_image.height))
            )
            output = io.BytesIO()
            crop.save(output, format="PNG", optimize=False)
            data = output.getvalue()
            digest = sha256_bytes(data)
            asset_id = asset_ids_by_digest.setdefault(digest, f"asset_{digest[:16]}")
            relative = f"assets/sha256-{digest}.png"
            if not any(item["asset_id"] == asset_id for item in assets):
                atomic_write_bytes(stage / relative, data)
                assets.append(
                    {
                        "schema_version": 1,
                        "asset_id": asset_id,
                        "sha256": digest,
                        "path": f"source/{relative}",
                        "mime_type": "image/png",
                        "width": crop.width,
                        "height": crop.height,
                        "generation": "raster_crop",
                        "page": provenance["page"],
                        "bbox": bbox,
                    }
                )
            asset_paths[asset_id] = relative
            assets_by_block[block.block_id] = asset_id
        page_image.close()
    revised = [
        replace(block, asset_refs=[assets_by_block[block.block_id]])
        if block.block_id in assets_by_block
        else block
        for block in blocks
    ]
    ratio = accounted_ink / total_ink if total_ink else 1.0
    return revised, assets, asset_paths, ratio


def _raster_crop_box(
    bbox: list[float], scale: float, image_size: tuple[int, int]
) -> tuple[int, int, int, int]:
    """Map a positive-area point bbox to a non-empty, clamped pixel crop."""
    width, height = image_size
    if width < 1 or height < 1:
        raise ValueError("PDF_RENDER_FAILED: rendered page image is empty")
    x0, y0, x1, y1 = bbox
    left = max(0, min(width - 1, math.floor(x0 * scale)))
    top = max(0, min(height - 1, math.floor(y0 * scale)))
    right = max(left + 1, min(width, math.ceil(x1 * scale)))
    bottom = max(top + 1, min(height, math.ceil(y1 * scale)))
    return left, top, right, bottom


def _page_ink_counts(
    objects: list[Any], page_image: Any, scale: float, policy: dict[str, Any]
) -> tuple[int, int]:
    try:
        from PIL import ImageChops, ImageDraw
    except ImportError as error:
        raise RuntimeError(
            "PDF_BACKEND_MISSING: Install paperweaver[pdf] to validate visible ink."
        ) from error
    threshold = int(policy["ink_luminance_threshold"])
    gray = page_image.convert("L")
    ink = mask = overlap = None
    try:
        ink = gray.point(lambda value: 255 if value < threshold else 0, mode="1").convert("L")
        mask = gray.copy().point(lambda _: 0)
        draw = ImageDraw.Draw(mask)
        for item in objects:
            x0, y0, x1, y1 = item.bbox
            draw.rectangle(
                (
                    max(0, round(x0 * scale) - 2),
                    max(0, round(y0 * scale) - 2),
                    min(mask.width, round(x1 * scale) + 2),
                    min(mask.height, round(y1 * scale) + 2),
                ),
                fill=255,
            )
        overlap = ImageChops.multiply(ink, mask)
        return _component_accounting(ink, overlap)
    finally:
        gray.close()
        for image in (ink, mask, overlap):
            if image is not None:
                image.close()


def _component_accounting(ink: Any, overlap: Any) -> tuple[int, int]:
    from PIL import Image

    size = (max(1, ink.width // 2), max(1, ink.height // 2))
    small_ink = ink.resize(size, Image.Resampling.NEAREST)
    small_overlap = overlap.resize(size, Image.Resampling.NEAREST)
    try:
        ink_data = small_ink.tobytes()
        overlap_data = small_overlap.tobytes()
        width, height = size
        visited = bytearray(width * height)
        accounted = total = 0
        for start, value in enumerate(ink_data):
            if value == 0 or visited[start]:
                continue
            queue = deque([start])
            visited[start] = 1
            area = covered = 0
            while queue:
                index = queue.popleft()
                area += 1
                if overlap_data[index]:
                    covered += 1
                x, y = index % width, index // width
                for neighbor in (
                    index - 1 if x else -1,
                    index + 1 if x + 1 < width else -1,
                    index - width if y else -1,
                    index + width if y + 1 < height else -1,
                ):
                    if neighbor >= 0 and ink_data[neighbor] and not visited[neighbor]:
                        visited[neighbor] = 1
                        queue.append(neighbor)
            if area < 2:
                continue
            total += area
            if covered / area >= 0.9:
                accounted += area
        return accounted, total
    finally:
        small_ink.close()
        small_overlap.close()


def _write_stage(
    stage: Path,
    run: PdfBackendRun,
    run_id: str,
    policy: dict[str, Any],
    blocks: list[PdfBlock],
    accounting: list[Any],
    markdown: str,
    article_map: list[Any],
    render_tree: dict[str, Any],
    qa: dict[str, Any],
    assets: list[dict[str, Any]],
    record: PaperSource,
    inventory: PaperInventory,
    manifest: dict[str, Any],
) -> None:
    run_root = stage / "pdf" / "runs" / run_id
    atomic_write_json(stage / "pdf" / "policy.json", policy)
    atomic_write_json(
        run_root / "backend.json",
        {"name": run.name, "version": run.version, "options": run.options},
    )
    raw = [
        {
            "schema_version": 1,
            "object_ref": item.object_ref,
            "object_kind": item.kind,
            "page": item.page,
            "bbox": item.bbox,
            "payload": item.payload,
            "attrs": item.attrs,
            "backend_ref": item.backend_ref,
        }
        for page in run.pages
        for item in page.objects
    ]
    _write_dict_jsonl(run_root / "raw-objects.jsonl", raw)
    _write_dict_jsonl(run_root / "base-blocks.jsonl", [item.to_dict() for item in blocks])
    _write_dict_jsonl(
        run_root / "base-relations.jsonl",
        [item.to_dict() for item in _element_relations(blocks)],
    )
    _write_dict_jsonl(
        run_root / "object-accounting.jsonl", [item.to_dict() for item in accounting]
    )
    _write_dict_jsonl(stage / "assets" / "manifest.jsonl", assets)
    atomic_write_text(stage / "article.md", markdown)
    _write_dict_jsonl(stage / "article-map.jsonl", [item.to_dict() for item in article_map])
    atomic_write_json(stage / "pdf" / "render-tree.json", render_tree)
    atomic_write_json(stage / "pdf" / "qa.json", qa)
    atomic_write_text(stage / "pdf" / "qa.md", render_qa_markdown(qa))
    atomic_write_json(stage / "inventory.json", inventory.to_dict())
    artifact_paths = {
        "backend": run_root / "backend.json",
        "raw_objects": run_root / "raw-objects.jsonl",
        "base_blocks": run_root / "base-blocks.jsonl",
        "base_relations": run_root / "base-relations.jsonl",
        "object_accounting": run_root / "object-accounting.jsonl",
        "asset_manifest": stage / "assets" / "manifest.jsonl",
        "article_map": stage / "article-map.jsonl",
        "render_tree": stage / "pdf" / "render-tree.json",
        "qa": stage / "pdf" / "qa.json",
    }
    manifest["artifacts"] = {
        name: {
            "path": path.relative_to(stage).as_posix(),
            "sha256": sha256_bytes(path.read_bytes()),
        }
        for name, path in artifact_paths.items()
    }
    atomic_write_json(stage / "pdf" / "manifest.json", manifest)
    atomic_write_json(stage / "source.json", record.to_dict())


def _element_relations(blocks: list[PdfBlock]) -> list[PdfRelation]:
    relations: list[PdfRelation] = []
    for index, block in enumerate(blocks):
        target: PdfBlock | None = None
        if block.kind == "table_caption" and index + 1 < len(blocks):
            candidate = blocks[index + 1]
            target = candidate if candidate.kind == "table" else None
        elif block.kind == "figure_caption" and index > 0:
            candidate = blocks[index - 1]
            target = candidate if candidate.kind == "figure" else None
        if target is None:
            continue
        relations.append(
            PdfRelation(
                1,
                stable_id("rel", "caption_of", block.block_id, target.block_id),
                "caption_of",
                [block.block_id],
                [target.block_id],
                {"label": block.text, "confidence": 1.0},
            )
        )
    return relations


def _write_dict_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_write_text(
        path,
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows),
    )


def _commit_stage(destination: Path, stage: Path, source_sha256: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    pending = destination / "pdf-import.pending.json"
    atomic_write_json(pending, {"schema_version": 1, "source_sha256": source_sha256})
    files = sorted(
        (path for path in stage.rglob("*") if path.is_file()),
        key=lambda path: (
            path.relative_to(stage).as_posix() in {"pdf/manifest.json", "source.json"},
            path.relative_to(stage).as_posix(),
        ),
    )
    for path in files:
        relative = path.relative_to(stage)
        atomic_write_bytes(destination / relative, path.read_bytes())
    pending.unlink(missing_ok=True)
