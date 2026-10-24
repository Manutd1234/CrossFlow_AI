"""Release bindings must match the reviewed carriageway and bundled graph.

These checks protect reviewed configuration, not camera accuracy. They never
contact a stream, start inference, or manufacture traffic observations.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from services.cctv_analysis import CameraConfig, inside_roi


def sources():
    registry = json.loads((ROOT / "backend/data/cctv_cameras.json").read_text())
    graph_bytes = (ROOT / "backend/data/batam_graph.json").read_bytes()
    review = json.loads((ROOT / "docs/verification/cctv-road-mapping-2026-10-24/mapping-review.json").read_text())
    return {c["camera_id"]: c for c in registry["cameras"]}, json.loads(graph_bytes), graph_bytes, review


@pytest.mark.parametrize("camera_id", ["13", "51", "48"])
def test_reviewed_binding_is_a_short_existing_directed_edge_in_this_graph(camera_id):
    cameras, graph, graph_bytes, review = sources()
    camera = cameras[camera_id]
    binding = next(row for row in review["bindings"] if row["camera_id"] == camera_id)
    config = CameraConfig.from_dict(camera["analysis_config"])
    policy = camera["estimation_config"]
    assert config.road_segment_id == binding["road_segment_id"]
    _, source, target, road_index = config.road_segment_id.split(":")
    edge = next(e for e in graph["adj"][source] if e[0] == target and e[2] == int(road_index))
    assert 0 < edge[1] <= 100
    assert policy["allow_routing"] is True
    assert policy["graph_revision"] == hashlib.sha256(graph_bytes).hexdigest() == review["graph_sha256"]
    a, b = graph["nodes"][source], graph["nodes"][target]
    bearing = math.degrees(math.atan2((b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2)), b[0] - a[0])) % 360
    lower, upper = binding["expected_bearing_range"]
    assert lower <= bearing <= upper
    assert binding["roi"] == camera["analysis_config"]["roi"]
    assert binding["opposed_direction_excluded"]
    assert f"https://www.openstreetmap.org/way/{binding['osm_source_way_id']}" in binding["primary_sources"]


@pytest.mark.parametrize("camera_id,in_lane,opposite,off_road", [
    ("13", (.45, .59), (.45, .49), (.45, .80)),
    ("51", (.60, .58), (.65, .38), (.15, .60)),
    ("48", (.80, .60), (.25, .60), (.53, .60)),
])
def test_revised_regions_keep_one_carriageway_and_exclude_opposition(camera_id, in_lane, opposite, off_road):
    cameras, _, _, _ = sources()
    roi = CameraConfig.from_dict(cameras[camera_id]["analysis_config"]).roi
    assert inside_roi(*in_lane, roi)
    assert not inside_roi(*opposite, roi)
    assert not inside_roi(*off_road, roi)


def test_mapping_never_promotes_detector_accuracy_or_unresolved_views():
    cameras, _, _, review = sources()
    assert len(cameras) == 28
    assert {row["camera_id"] for row in review["bindings"]} == {"13", "51", "48"}
    for camera_id in ("1", "19"):
        assert cameras[camera_id]["estimation_config"]["allow_routing"] is False
        assert cameras[camera_id]["analysis_config"]["road_segment_id"] is None
    # Cars parked on the civic plaza are not public road congestion evidence.
    assert cameras["1"]["estimation_config"]["enabled"] is False
    assert cameras["1"]["analysis_config"]["enabled"] is False
    assert cameras["1"]["analysis_config"]["analysis_authorized"] is True
    # A recovered source must not reactivate a legacy, unreviewed direction.
    assert cameras["15"]["estimation_config"]["allow_routing"] is False
    assert cameras["15"]["analysis_config"]["road_segment_id"] is not None
    for camera in cameras.values():
        config = CameraConfig.from_dict(camera["analysis_config"])
        assert config.thresholds_validated is False
        assert config.independent_validation_reference is None
        assert config.validated_detector_id is None
    assert review["media_retained"] is False
    assert review["independent_detector_validation_added"] is False
    assert review["permissions_changed"] is False
