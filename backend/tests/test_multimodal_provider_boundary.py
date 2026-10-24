"""Untrusted routing payloads must fail into labelled continuity estimates."""

import copy
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import multimodal_router as routing


ORIGIN = (1.3521, 103.8198)
DESTINATION = (1.2966, 103.8547)


def valid_payload():
    return {
        "code": "Ok", "routes": [{
            "distance": 8000, "duration": 900,
            "geometry": {"coordinates": [[103.8198, 1.3521], [103.8547, 1.2966]]},
            "legs": [{"steps": [
                {"distance": 8000, "name": "Mapped road", "maneuver": {
                    "type": "depart", "location": [103.8198, 1.3521],
                }},
                {"distance": 0, "maneuver": {
                    "type": "arrive", "location": [103.8547, 1.2966],
                }},
            ]}],
        }],
    }


def route(payload):
    response = io.BytesIO(json.dumps(payload).encode())
    with patch.object(routing.urllib.request, "urlopen", return_value=response):
        return routing._osrm_route(ORIGIN, DESTINATION)


def test_valid_geometry_and_zero_length_arrival_remain_mapped():
    result = route(valid_payload())
    assert result["distance_km"] == 8
    assert result["duration_mins"] == 15
    assert result["geometry"] == [list(ORIGIN), list(DESTINATION)]
    assert result["navigation"]["maneuvers"][-1]["distance_m"] == 0
    assert result["is_estimate"] is False


@pytest.mark.parametrize("path,value", [
    (("routes", 0), None),
    (("routes", 0, "geometry"), None),
    (("routes", 0, "geometry", "coordinates"), [[103, 1], [104, float("nan")]]),
    (("routes", 0, "geometry", "coordinates"), [[103, 1], [181, 1]]),
    (("routes", 0, "geometry", "coordinates"), [[103, 1], [True, 1]]),
    (("routes", 0, "geometry", "coordinates"), [[103, 1], [104]]),
    (("routes", 0, "distance"), float("inf")),
    (("routes", 0, "distance"), -10),
    (("routes", 0, "duration"), "900"),
    (("routes", 0, "duration"), True),
    (("routes", 0, "duration"), 0),
    (("routes", 0, "legs"), None),
    (("routes", 0, "legs"), [None]),
    (("routes", 0, "legs", 0, "steps"), [None, None]),
    (("routes", 0, "legs", 0, "steps"), []),
    (("routes", 0, "legs", 0, "steps", 0, "maneuver", "location"), None),
    (("routes", 0, "legs", 0, "steps", 0, "maneuver", "location"), [103, 91]),
    (("routes", 0, "legs", 0, "steps", 0, "distance"), float("nan")),
])
def test_malformed_provider_leg_does_not_crash_or_claim_mapped_directions(path, value):
    payload = copy.deepcopy(valid_payload())
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert route(payload) is None


def test_response_is_read_with_a_finite_size_limit():
    class OversizedResponse(io.BytesIO):
        def read(self, size=-1):
            assert size == routing._MAX_OSRM_RESPONSE_BYTES + 1
            return b" " * size

    with patch.object(routing.urllib.request, "urlopen", return_value=OversizedResponse()):
        assert routing._osrm_route(ORIGIN, DESTINATION) is None


def test_invalid_payload_keeps_the_road_leg_explicitly_estimated():
    with patch.object(routing.urllib.request, "urlopen", return_value=io.BytesIO(b'{"routes":[null]}')):
        result = routing._road_leg("SINGAPORE", ORIGIN, DESTINATION, "Start", "End", "COMMUTER", "BALANCED")
    assert result["is_estimate"] is True
    assert result["navigation"]["data_source"] == "offline_access_estimate"
