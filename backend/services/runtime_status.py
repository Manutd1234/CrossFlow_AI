"""Secret-free integration metadata for the protected diagnostics endpoint.

Configuration is not a successful remote round trip. This snapshot performs no
provider requests and reports last-operation availability separately; callers
must still verify real account login, SMTP delivery and hosted persistence.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Mapping

from auth import identity
from services import ferry_freshness_store, ferry_schedule
from services import google_routes_benchmark, historical_store, route_run_store, traffic_upstream


_LOGGER = logging.getLogger(__name__)


def _fields(payload: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    """Allowlist output so adding a storage path/key upstream cannot leak it."""
    return {name: payload[name] for name in names if name in payload}


def _metadata(read: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
    try:
        return {"metadata_available": True, **read()}
    except Exception as error:
        _LOGGER.warning(
            "[integration_status] metadata unavailable (%s)", type(error).__name__,
        )
        return {"metadata_available": False, "failure": "metadata_unavailable"}


def snapshot(
    *,
    route_store: Any,
    route_learning_store: Any,
    graph_revision: str,
    graph_meta: Mapping[str, Any],
    model_metrics: Mapping[str, Any],
    intelligence_status: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine existing stores' metadata without network or model inference."""
    local_routes = _metadata(lambda: _fields(route_store.storage_metadata(), (
        "source", "shared", "durable", "durability", "scope",
    )))
    congestion_history = _metadata(lambda: _fields(
        historical_store.get_storage_metadata(), (
            "engine", "durability", "durable", "shared_across_instances",
            "fallback_to_memory",
        ),
    ))
    traversals = _metadata(lambda: _fields(
        route_learning_store.status(graph_revision), (
            "enabled", "storage_backend", "storage_location", "durable",
            "graph_revision", "learning_revision",
            "current_graph_observation_count", "stale_graph_observation_count",
            "qualifying_edge_count",
        ),
    ))
    shared_routes = _metadata(lambda: {
        "configured": route_run_store.configured(),
        "last_operation_available": route_run_store.available(),
        "storage_backend": "supabase_service_role_rest",
        "durability_if_available": "shared_database",
    })
    ferry_freshness = _metadata(lambda: {
        "configured": ferry_freshness_store.configured(),
        "required": ferry_freshness_store.required(),
        "last_operation_available": ferry_freshness_store.available(),
        "committed_fallback_enabled": ferry_schedule.committed_snapshot_fallback_enabled(),
        "live_operations_available": False,
    })
    auth = _metadata(lambda: _fields(identity.status(), (
        "mode", "enabled", "configured", "sign_in",
    )))
    model = _metadata(lambda: {
        **_fields(model_metrics, (
            "is_trained", "training_data_source", "validation_scope",
            "total_samples", "observed_training_rows", "observed_fit_rows",
            "r2_score", "mae", "rmse",
        )),
        "calibration": _fields(model_metrics.get("calibration") or {}, (
            "interval", "interval_coverage_target", "interval_coverage_observed",
            "holdout_order", "holdout_samples",
        )),
    })
    tomtom_configured = bool(os.environ.get("TOMTOM_API_KEY", "").strip())
    upstream_configured = traffic_upstream.configured()
    return {
        "contract_version": 1,
        "snapshot_scope": "configuration_and_local_metadata",
        "additional_remote_probes_performed": False,
        "production_readiness": "not_assessed",
        "routing": {
            "engine": "vehicle_constrained_openstreetmap_astar",
            "graph_revision": graph_revision,
            "graph_schema_version": graph_meta.get("schema_version"),
            "graph_node_count": graph_meta.get("node_count"),
        },
        "authentication": auth,
        "persistence": {
            "local_routes": local_routes,
            "shared_routes": shared_routes,
            "congestion_history": congestion_history,
            "verified_traversals": traversals,
            "routing_intelligence": _fields(intelligence_status, (
                "enabled", "configured", "implementation", "schema_health",
                "schema_checked_at", "verified_available",
                "durable_spatial_history", "durable_inactive_review_queue",
            )),
            "ferry_freshness": ferry_freshness,
        },
        "providers": {
            "tomtom": {
                "configured": tomtom_configured,
                "upstream_configured": upstream_configured,
                "configured_source": (
                    "direct_tomtom" if tomtom_configured else
                    "public_crossflow_vercel_upstream" if upstream_configured else
                    "simulated"
                ),
                "current_response_source": "not_sampled",
                "upstream_retention": "process_memory_current_snapshot_only",
                "fallback_source": "simulated",
            },
            "google_route_benchmark": {
                "enabled": google_routes_benchmark.is_enabled(),
                "configured": bool(google_routes_benchmark.get_api_key()),
                "current_response_source": "not_sampled",
                "external_route_content_persisted": False,
            },
            "ferry_operations": {
                "source": "published_timetable",
                "live_operations_available": False,
            },
        },
        "model": model,
        "limitations": [
            "Configured credentials do not prove remote schema, login or email delivery.",
            "Last-operation availability is not a new health probe.",
            "Local metadata does not certify a writable database or shared durability.",
            "Observed training rows and holdout scope do not establish field accuracy.",
        ],
    }
