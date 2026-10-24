"""Bounded, unambiguous JSON at external provider response boundaries."""
import json
import math


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate provider JSON field")
        result[key] = value
    return result


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite provider JSON value")
    return number


def _constant(_value):
    raise ValueError("Non-standard provider JSON constant")


def read_json(response, *, max_bytes):
    """Read at most the budget plus one sentinel byte; never log raw content.

    Socket timeouts remain the responsibility of each provider adapter. Values
    still require that adapter's domain/shape validation after decoding.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 8 * 1024 * 1024:
        raise ValueError("Invalid provider response budget")
    raw = response.read(max_bytes + 1)
    if not isinstance(raw, bytes) or len(raw) > max_bytes:
        raise ValueError("Provider response exceeds byte budget")
    try:
        return json.loads(raw, object_pairs_hook=_object, parse_float=_float, parse_constant=_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Invalid provider JSON") from error
