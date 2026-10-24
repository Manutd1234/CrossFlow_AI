"""Nearest OSM segment attributes, with distances and missingness kept explicit.

OSM geometry is mapped data, not a field survey of capacity. Never invent lane
counts, speed limits or vehicle/hour capacity when a source tag is absent.
"""
from functools import lru_cache
import math
import numpy as np


@lru_cache(maxsize=1)
def _index():
    from services import router
    records = [(source, edge) for source, edges in router.ROAD_ADJ.items()
               for edge in edges if source < edge.target or not any(
                   other.target == source and other.road_index == edge.road_index
                   for other in router.ROAD_ADJ.get(edge.target, ())) ]
    start = np.array([router.NODES[source] for source, _ in records])
    end = np.array([router.NODES[edge.target] for _, edge in records])
    return records, start, end


@lru_cache(maxsize=512)
def road_features(latitude: float, longitude: float) -> dict:
    if not math.isfinite(latitude) or not math.isfinite(longitude) or not .88 <= latitude <= 1.24 or not 103.75 <= longitude <= 104.30:
        raise ValueError("Road point is outside supported Batam bounds.")
    from services import router
    records, start, end = _index()
    scale = np.array([111_195., 111_195. * math.cos(math.radians(latitude))])
    a = (start - [latitude, longitude]) * scale
    delta = (end - start) * scale
    length2 = (delta * delta).sum(axis=1)
    fraction = np.clip(-(a * delta).sum(axis=1) / np.maximum(length2, 1e-8), 0, 1)
    distance = np.sqrt(((a + fraction[:, None] * delta) ** 2).sum(axis=1))
    index = int(np.argmin(distance))
    source, edge = records[index]
    matched = distance[index] <= 80
    terminal_nodes = [router.LANDMARKS[name] for name in ('batam_centre', 'sekupang', 'harbour_bay')]
    terminal_distance = min(math.hypot((router.NODES[node][0] - latitude) * scale[0],
                                     (router.NODES[node][1] - longitude) * scale[1]) for node in terminal_nodes) / 1000
    return {
        "matched": bool(matched), "match_distance_m": round(float(distance[index]), 2),
        "method": "nearest_segment_undirected_not_provider_direction_map_match",
        "road_name": edge.name if matched else None,
        "road_class": edge.highway if matched else None,
        "segment_length_m": round(float(np.sqrt(length2[index])), 2) if matched else None,
        "lanes": edge.lanes if matched else None,
        "posted_maxspeed_kph": edge.maxspeed_kph if matched else None,
        "capacity_vph": None,
        "nearest_mapped_terminal_km": round(terminal_distance, 4),
        "osm_edge": [source, edge.target, edge.road_index] if matched else None,
        "graph_revision": router.GRAPH_REVISION,
        "source": "OpenStreetMap", "license": "ODbL 1.0",
        "source_url": "https://www.openstreetmap.org/copyright",
        "graph_generated_at": router.GRAPH_META.get('generated_at'),
    }


ROAD_FEATURE_NAMES = ('mapped_segment_length_m', 'mapped_lanes', 'lanes_missing',
                      'posted_speed_limit_kph', 'speed_limit_missing', 'road_match_missing',
                      'nearest_mapped_terminal_km',
                      *(f'road_{name}' for name in ('primary', 'secondary', 'tertiary', 'residential', 'other')))


def feature_vector(road: dict) -> tuple[float, ...]:
    kind = road.get('road_class')
    return (road.get('segment_length_m') or 0., road.get('lanes') or 0., float(road.get('lanes') is None),
            road.get('posted_maxspeed_kph') or 0., float(road.get('posted_maxspeed_kph') is None),
            float(not road.get('matched')), road.get('nearest_mapped_terminal_km') or 0.,
            *(float(kind == name if name != 'other' else kind not in ('primary', 'secondary', 'tertiary', 'residential'))
              for name in ('primary', 'secondary', 'tertiary', 'residential', 'other')))
