from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class OverlappingGroup:
    primary_indices: list[int]
    context_indices: list[int]

    @property
    def all_indices(self) -> list[int]:
        return [*self.primary_indices, *self.context_indices]


def overlapping_groups(
    vectors: list[list[float]], *, max_topics: int, overlap_ratio: float
) -> list[OverlappingGroup]:
    """Build disjoint primary partitions and add nearest external context to each partition."""
    if not vectors:
        return []
    if max_topics < 2:
        raise ValueError("max_topics must be at least 2")
    if not 0 <= overlap_ratio < 0.5:
        raise ValueError("overlap_ratio must be in [0, 0.5)")
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or len(matrix) != len(vectors) or not np.isfinite(matrix).all():
        raise ValueError("invalid direction grouping embeddings")
    norms = np.linalg.norm(matrix, axis=1)
    if np.any(norms == 0):
        raise ValueError("direction grouping embeddings cannot be zero")
    matrix = matrix / norms[:, None]
    context_capacity = int(round(max_topics * overlap_ratio))
    primary_capacity = max(1, max_topics - context_capacity)
    partitions: list[list[int]] = []

    def split(indices: list[int]) -> None:
        if len(indices) <= primary_capacity:
            partitions.append(indices)
            return
        data = matrix[indices]
        center = data.mean(axis=0)
        left_seed = int(np.argmin(data @ center))
        right_seed = int(np.argmin(data @ data[left_seed]))
        centers = data[[left_seed, right_seed]].copy()
        mask = np.zeros(len(indices), dtype=bool)
        for _ in range(16):
            updated_mask = (data @ centers[1]) > (data @ centers[0])
            if not updated_mask.any() or updated_mask.all():
                break
            mask = updated_mask
            updated = np.stack([data[~mask].mean(axis=0), data[mask].mean(axis=0)])
            updated /= np.maximum(np.linalg.norm(updated, axis=1)[:, None], 1e-12)
            if np.allclose(centers, updated):
                break
            centers = updated
        left = [index for index, right in zip(indices, mask, strict=True) if not right]
        right = [index for index, right in zip(indices, mask, strict=True) if right]
        if not left or not right:
            middle = len(indices) // 2
            left, right = indices[:middle], indices[middle:]
        split(left)
        split(right)

    split(list(range(len(vectors))))
    partitions = _merge_small_partitions(partitions, matrix, primary_capacity)
    groups = []
    universe = set(range(len(vectors)))
    for primary in sorted(partitions, key=lambda values: min(values)):
        outside = sorted(universe - set(primary))
        available = max_topics - len(primary)
        wanted = min(context_capacity, available, len(outside))
        if wanted:
            similarities = matrix[outside] @ matrix[primary].T
            scores = similarities.max(axis=1)
            ranked = sorted(
                zip(outside, scores, strict=True), key=lambda item: (-float(item[1]), item[0])
            )
            context = [index for index, _ in ranked[:wanted]]
        else:
            context = []
        groups.append(OverlappingGroup(primary_indices=primary, context_indices=context))
    return groups


def duplicate_components(
    vectors: list[list[float]], *, minimum_similarity: float
) -> list[list[int]]:
    """Return connected components containing at least one possible duplicate pair."""
    if not vectors:
        return []
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    if matrix.ndim != 2 or np.any(norms == 0) or not np.isfinite(matrix).all():
        raise ValueError("invalid direction deduplication embeddings")
    matrix = matrix / norms[:, None]
    similarities = matrix @ matrix.T
    unseen = set(range(len(vectors)))
    components = []
    while unseen:
        start = min(unseen)
        stack = [start]
        component = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            neighbors = {
                int(index)
                for index in np.flatnonzero(similarities[current] >= minimum_similarity)
                if int(index) != current
            }
            stack.extend(sorted(neighbors - component, reverse=True))
        unseen -= component
        if len(component) > 1:
            components.append(sorted(component))
    return components


def _merge_small_partitions(
    partitions: list[list[int]], matrix: np.ndarray, capacity: int
) -> list[list[int]]:
    partitions = [sorted(values) for values in partitions]
    while True:
        best: tuple[float, int, int] | None = None
        for left in range(len(partitions)):
            for right in range(left + 1, len(partitions)):
                if len(partitions[left]) + len(partitions[right]) > capacity:
                    continue
                left_center = matrix[partitions[left]].mean(axis=0)
                right_center = matrix[partitions[right]].mean(axis=0)
                score = float(
                    left_center
                    @ right_center
                    / max(np.linalg.norm(left_center) * np.linalg.norm(right_center), 1e-12)
                )
                candidate = (score, -left, -right)
                if best is None or candidate > best:
                    best = candidate
        if best is None:
            return partitions
        _, negative_left, negative_right = best
        left, right = -negative_left, -negative_right
        merged = sorted(partitions[left] + partitions[right])
        partitions = [
            values for index, values in enumerate(partitions) if index not in {left, right}
        ]
        partitions.append(merged)
