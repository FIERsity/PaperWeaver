"""Deterministic clustering and caption association for PDF visual objects."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VisualCluster:
    objects: tuple[Any, ...]
    bbox: tuple[float, float, float, float]

    @property
    def object_refs(self) -> set[str]:
        return {item.object_ref for item in self.objects}


def cluster_visual_objects(
    page_objects: list[Any], page_width: float, page_height: float, *, gap: float = 3.0
) -> list[VisualCluster]:
    """Cluster image/vector primitives on a coarse deterministic geometry grid."""
    objects = [
        item
        for item in page_objects
        if item.kind in {"image_occurrence", "line", "rect", "curve"}
    ]
    if not objects:
        return []
    cell = max(gap, 1.0)
    parent = list(range(len(objects)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    occupied: dict[tuple[int, int], int] = {}
    for index, item in enumerate(objects):
        x0, y0, x1, y1 = item.bbox
        x0 = max(0.0, x0 - gap)
        y0 = max(0.0, y0 - gap)
        x1 = min(page_width, x1 + gap)
        y1 = min(page_height, y1 + gap)
        left = max(0, math.floor(x0 / cell))
        right = max(left, math.floor(x1 / cell))
        top = max(0, math.floor(y0 / cell))
        bottom = max(top, math.floor(y1 / cell))
        for x in range(left, right + 1):
            for y in range(top, bottom + 1):
                key = (x, y)
                if key in occupied:
                    union(index, occupied[key])
                else:
                    occupied[key] = index

    groups: dict[int, list[Any]] = {}
    for index, item in enumerate(objects):
        groups.setdefault(find(index), []).append(item)
    clusters = [
        VisualCluster(tuple(items), tuple(_union_bbox([item.bbox for item in items])))
        for items in groups.values()
    ]
    return sorted(clusters, key=lambda item: (item.bbox[1], item.bbox[0]))


def decorative_cluster(cluster: VisualCluster, rule_thickness: float) -> bool:
    if any(item.kind == "image_occurrence" for item in cluster.objects):
        return False
    x0, y0, x1, y1 = cluster.bbox
    width, height = x1 - x0, y1 - y0
    if len(cluster.objects) == 1 and min(width, height) <= rule_thickness * 1.5:
        return True
    return len(cluster.objects) <= 3 and min(width, height) <= rule_thickness


def figure_clusters(
    clusters: list[VisualCluster],
    caption_bbox: list[float],
    claimed_refs: set[str],
    *,
    max_gap: float,
) -> list[VisualCluster]:
    """Select the nearest coherent visual band above, or then below, a caption."""
    available = [item for item in clusters if not item.object_refs & claimed_refs]
    if not available:
        return []
    cx0, cy0, cx1, cy1 = caption_bbox
    caption_width = max(cx1 - cx0, 1.0)

    def horizontal_overlap(item: VisualCluster) -> float:
        x0, _, x1, _ = item.bbox
        return max(0.0, min(x1, cx1) - max(x0, cx0)) / min(
            max(x1 - x0, 1.0), caption_width
        )

    above = [
        (max(0.0, cy0 - item.bbox[3]), item)
        for item in available
        if item.bbox[3] <= cy1 and horizontal_overlap(item) >= 0.15
    ]
    below = [
        (max(0.0, item.bbox[1] - cy1), item)
        for item in available
        if item.bbox[1] >= cy0 and horizontal_overlap(item) >= 0.15
    ]
    candidates = above if above and min(value[0] for value in above) <= max_gap else below
    if not candidates:
        return []
    nearest = min(value[0] for value in candidates)
    if nearest > max_gap:
        return []
    primary = min(candidates, key=lambda value: value[0])[1]
    selected = [primary]
    changed = True
    while changed:
        changed = False
        for _distance, item in candidates:
            if item in selected:
                continue
            iy0, iy1 = item.bbox[1], item.bbox[3]
            if any(
                max(0.0, min(existing.bbox[3], iy1) - max(existing.bbox[1], iy0)) > 0
                or min(
                    abs(iy0 - existing.bbox[3]),
                    abs(existing.bbox[1] - iy1),
                )
                <= 15.0
                for existing in selected
            ):
                selected.append(item)
                changed = True
    return selected


def _union_bbox(boxes: list[list[float]]) -> list[float]:
    return [
        round(min(box[0] for box in boxes), 4),
        round(min(box[1] for box in boxes), 4),
        round(max(box[2] for box in boxes), 4),
        round(max(box[3] for box in boxes), 4),
    ]
