import numpy as np

from track_insight.directions.clustering import duplicate_components, overlapping_groups


def test_overlapping_groups_cover_each_primary_once_and_add_context():
    vectors = []
    for index in range(24):
        angle = index / 24 * np.pi * 2
        vectors.append([float(np.cos(angle)), float(np.sin(angle))])

    groups = overlapping_groups(vectors, max_topics=10, overlap_ratio=0.2)

    primary = [index for group in groups for index in group.primary_indices]
    context = [index for group in groups for index in group.context_indices]
    assert sorted(primary) == list(range(24))
    assert len(primary) == len(set(primary))
    assert context
    assert all(len(group.all_indices) <= 10 for group in groups)
    assert all(not set(group.primary_indices) & set(group.context_indices) for group in groups)


def test_duplicate_components_only_return_connected_candidates():
    vectors = [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0], [0.01, 0.99], [-1.0, 0.0]]

    assert duplicate_components(vectors, minimum_similarity=0.95) == [[0, 1], [2, 3]]
