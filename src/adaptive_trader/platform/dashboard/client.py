"""Read-only server-side client for the private control API."""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from adaptive_trader.platform.canonical import JsonValue, canonical_json_bytes
from adaptive_trader.platform.config import RuntimeService, RuntimeSettings
from adaptive_trader.platform.security import RedactedSecret

_MAX_API_RESPONSE_BYTES = 1_048_576
_MAX_CONTAINER_ITEMS = 512
_MAX_NESTING_DEPTH = 8
_MAX_TEXT_LENGTH = 16_384
_ALLOWED_API_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "control-api"})
_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "connection_string",
    "cookie",
    "credential",
    "database_url",
    "password",
    "private_key",
    "secret",
    "token",
)
_SENSITIVE_COMPACT_FRAGMENTS = ("apikey", "connectionstring", "databaseurl", "privatekey")
_SECRET_TEXT = re.compile(
    r"(?:bearer\s+\S+|-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----|"
    r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@|"
    r"(?:secret|password|token|authorization|credential|api[-_.]?key|"
    r"private[-_.]?key|cookie|connection[-_.]?string|database[-_.]?url)\s*[:=])",
    re.ASCII | re.IGNORECASE,
)
_JOB_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$", re.ASCII)
_ARTIFACT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$", re.ASCII)
_JOB_FIELDS = frozenset(
    {
        "job_id",
        "job_type",
        "state",
        "attempt_count",
        "max_attempts",
        "next_attempt_at",
        "lease_expires_at",
        "completed_at",
        "safe_last_error_code",
        "safe_last_error_message",
        "result_artifact_id",
        "created_at",
        "updated_at",
        "version",
    }
)


class DashboardApiError(RuntimeError):
    """A safe dashboard-facing API failure without response or credential details."""


class DashboardRoute(StrEnum):
    HEALTH_LIVE = "/health/live"
    HEALTH_READY = "/health/ready"
    SYSTEM_STATUS = "/v1/system/status"
    EXPERIMENT = "/v1/experiment"
    DATA_STATUS = "/v1/data/status"
    DATA_GAPS = "/v1/data/gaps"
    DATASETS = "/v1/datasets"
    DECISION_SLOTS = "/v1/decision-slots"
    SIGNALS = "/v1/signals"
    RISK_DECISIONS = "/v1/risk-decisions"
    RISK_LATCHES = "/v1/risk/latches"
    ORDERS = "/v1/orders"
    FILLS = "/v1/fills"
    RECONCILIATIONS = "/v1/reconciliations"
    INCIDENTS = "/v1/incidents"
    AUDIT_STATUS = "/v1/audit/status"


_PAGE_FIELDS: dict[DashboardRoute, frozenset[str]] = {
    DashboardRoute.SYSTEM_STATUS: frozenset({"broker_authority", "database", "service"}),
    DashboardRoute.EXPERIMENT: frozenset(
        {
            "configuration",
            "experiment_content_hash",
            "experiment_hash",
            "experiment_id",
            "experiment_symbol_id",
            "experiment_version",
            "ordinal",
            "registered_at",
            "role",
            "schema_version",
            "symbol",
            "symbol_content_hash",
        }
    ),
    DashboardRoute.DATA_STATUS: frozenset(
        {
            "basket_watermark_id",
            "component_hash",
            "contiguous_through",
            "experiment_hash",
            "role",
            "status",
            "timeframe",
            "updated_at",
            "version",
        }
    ),
    DashboardRoute.DATA_GAPS: frozenset(
        {
            "adjustment",
            "attempt_count",
            "detected_at",
            "experiment_hash",
            "feed",
            "gap_end_at",
            "gap_id",
            "gap_start_at",
            "last_attempt_at",
            "provider",
            "reason_code",
            "resolved_at",
            "status",
            "symbol",
            "timeframe",
            "version",
        }
    ),
    DashboardRoute.DATASETS: frozenset(
        {
            "adjustment",
            "artifact_id",
            "correction_summary",
            "created_at",
            "dataset_id",
            "dirty_worktree",
            "experiment_hash",
            "feed",
            "gap_summary",
            "logical_hash",
            "manifest_hash",
            "physical_hash",
            "promotable",
            "provider",
            "range_end_at",
            "range_start_at",
            "roles",
            "row_counts",
            "schema_version",
            "source_git_commit",
            "status",
            "symbols",
            "timeframe",
            "uv_lock_hash",
        }
    ),
    DashboardRoute.DECISION_SLOTS: frozenset(
        {
            "attempt_count",
            "claim_owner",
            "claimed_at",
            "completed_at",
            "correlation_id",
            "created_at",
            "deadline_at",
            "decision_type",
            "experiment_hash",
            "experiment_id",
            "experiment_version",
            "lease_expires_at",
            "ready_at",
            "reason_code",
            "required_completion_at",
            "session_date",
            "signal_provider_id",
            "signal_provider_version",
            "slot_id",
            "source_interval_end",
            "source_interval_start",
            "state",
            "updated_at",
            "version",
        }
    ),
    DashboardRoute.SIGNALS: frozenset(
        {
            "actions",
            "active_symbols",
            "artifact_hash",
            "artifact_id",
            "availability_mask",
            "content_hash",
            "contract_version",
            "correlation_id",
            "created_at",
            "data_contract_hash",
            "expected_edge_bps",
            "experiment_hash",
            "experiment_id",
            "experiment_version",
            "expires_at",
            "paper_submission_eligible",
            "policy_hash",
            "promotable",
            "proposed_signed_target_inputs",
            "provider_id",
            "provider_source_mode",
            "provider_version",
            "signal_id",
            "slot_id",
            "source_bar_end",
        }
    ),
    DashboardRoute.RISK_DECISIONS: frozenset(
        {
            "approved_targets",
            "cash_weight",
            "content_hash",
            "controls",
            "decided_at",
            "experiment_hash",
            "gross_exposure",
            "net_exposure",
            "policy_id",
            "policy_version",
            "reason_codes",
            "risk_decision_id",
            "signal_id",
            "slot_id",
        }
    ),
    DashboardRoute.RISK_LATCHES: frozenset(
        {
            "action",
            "actor",
            "content_hash",
            "experiment_hash",
            "latch_event_id",
            "latch_type",
            "occurred_at",
            "reason_code",
            "sequence",
        }
    ),
    DashboardRoute.ORDERS: frozenset(
        {
            "accepted_at",
            "average_fill_price",
            "broker_order_id",
            "client_order_id",
            "correlation_id",
            "created_at",
            "cumulative_filled_quantity",
            "deadline_at",
            "effect",
            "execution_plan_id",
            "experiment_hash",
            "final_target_quantity",
            "forced_flat",
            "last_event_sequence",
            "notional",
            "order_intent_id",
            "phase",
            "quantity",
            "reference_price",
            "risk_decision_id",
            "safe_error_code",
            "sequence",
            "side",
            "state",
            "submitted_at",
            "symbol",
            "target_version",
            "updated_at",
            "version",
        }
    ),
    DashboardRoute.FILLS: frozenset(
        {
            "broker_execution_id",
            "client_order_id",
            "content_hash",
            "fee",
            "fill_id",
            "occurred_at",
            "price",
            "quantity",
            "side",
            "symbol",
        }
    ),
    DashboardRoute.RECONCILIATIONS: frozenset(
        {
            "account_id_hash",
            "completed_at",
            "content_hash",
            "correlation_id",
            "discrepancies",
            "execution_plan_id",
            "expected_cash",
            "expected_equity",
            "expected_positions",
            "experiment_hash",
            "fill_hashes",
            "observed_cash",
            "observed_equity",
            "observed_positions",
            "order_hashes",
            "reconciliation_id",
            "slot_id",
            "started_at",
            "status",
        }
    ),
    DashboardRoute.INCIDENTS: frozenset(
        {
            "experiment_hash",
            "idempotency_key",
            "incident_id",
            "incident_type",
            "opened_at",
            "reason_code",
            "resolved_at",
            "severity",
            "status",
            "version",
        }
    ),
    DashboardRoute.AUDIT_STATUS: frozenset(
        {"actor", "event_hash", "event_type", "occurred_at", "sequence", "stream_id"}
    ),
}
_INTEGER_FIELDS = frozenset(
    {
        "attempt_count",
        "contract_version",
        "experiment_version",
        "last_event_sequence",
        "ordinal",
        "policy_version",
        "schema_version",
        "sequence",
        "target_version",
        "version",
    }
)
_BOOLEAN_FIELDS = frozenset(
    {"dirty_worktree", "forced_flat", "paper_submission_eligible", "promotable"}
)
_JSON_CONTAINER_FIELDS = frozenset(
    {
        "actions",
        "active_symbols",
        "approved_targets",
        "availability_mask",
        "configuration",
        "controls",
        "correction_summary",
        "discrepancies",
        "expected_edge_bps",
        "expected_positions",
        "fill_hashes",
        "gap_summary",
        "observed_positions",
        "order_hashes",
        "proposed_signed_target_inputs",
        "reason_codes",
        "roles",
        "row_counts",
        "symbols",
    }
)


class _Response(Protocol):
    status: int
    headers: object

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...


class _Opener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> _Response: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects so a bearer header is never replayed to another origin."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


class DashboardApiClient:
    """Fetch only enumerated GET routes from the configured internal API origin."""

    def __init__(
        self,
        *,
        base_url: str,
        token: RedactedSecret,
        opener: _Opener | None = None,
    ) -> None:
        self._base_url = _validated_base_url(base_url)
        if type(token) is not RedactedSecret:
            raise TypeError("dashboard API client requires a loaded token")
        revealed = token.reveal()
        if len(revealed) < 32 or len(revealed) > 4_096 or not revealed.isascii():
            raise DashboardApiError("dashboard API token is invalid")
        self._token = token
        self._opener = opener or cast(
            _Opener, build_opener(ProxyHandler({}), _NoRedirectHandler()).open
        )

    @classmethod
    def from_settings(
        cls,
        settings: RuntimeSettings,
        *,
        opener: _Opener | None = None,
    ) -> DashboardApiClient:
        if (
            type(settings) is not RuntimeSettings
            or settings.service is not RuntimeService.DASHBOARD
        ):
            raise TypeError("dashboard API client requires dashboard runtime settings")
        if settings.operator_token_file is None:
            raise DashboardApiError("dashboard API token is not configured")
        return cls(
            base_url=settings.api_base_url,
            token=settings.operator_token_file.load(),
            opener=opener,
        )

    def get(self, route: DashboardRoute) -> dict[str, JsonValue]:
        if type(route) is not DashboardRoute:
            raise TypeError("dashboard route must use the closed contract")
        response = self._get_json(route.value)
        _validate_route_response(route, response)
        return response

    def get_job(self, job_id: str) -> dict[str, JsonValue]:
        """Read one exact job identifier without granting a mutation capability."""

        if type(job_id) is not str or _JOB_ID.fullmatch(job_id) is None:
            raise DashboardApiError("job identifier is invalid")
        response = self._get_json(f"/v1/jobs/{job_id}")
        _validate_job_response(job_id, response)
        return response

    def _get_json(self, path: str) -> dict[str, JsonValue]:
        request = Request(
            f"{self._base_url}{path}",
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token.reveal()}",
            },
        )
        try:
            with self._opener(request, timeout=5.0) as wire_response:
                if wire_response.status != 200:
                    raise DashboardApiError("control API request was unsuccessful")
                content_type_reader = getattr(wire_response.headers, "get_content_type", None)
                if not callable(content_type_reader) or content_type_reader() != "application/json":
                    raise DashboardApiError("control API response was invalid")
                payload = wire_response.read(_MAX_API_RESPONSE_BYTES + 1)
        except DashboardApiError:
            raise
        except (HTTPError, TimeoutError, URLError, OSError, ValueError):
            raise DashboardApiError("control API request failed") from None
        if len(payload) > _MAX_API_RESPONSE_BYTES:
            raise DashboardApiError("control API response exceeded the size limit")
        try:
            decoded = json.loads(payload, object_pairs_hook=_unique_object)
            canonical_json_bytes(decoded)
        except (RecursionError, TypeError, UnicodeError, ValueError):
            raise DashboardApiError("control API response was invalid") from None
        if type(decoded) is not dict:
            raise DashboardApiError("control API response was invalid")
        return cast(dict[str, JsonValue], decoded)


def _validate_job_response(job_id: str, response: dict[str, JsonValue]) -> None:
    _validate_json_tree(response, depth=0)
    if frozenset(response) != _JOB_FIELDS or response["job_id"] != job_id:
        raise DashboardApiError("control API response was invalid")
    if response["job_type"] not in (
        "DATA_QUALITY_AUDIT",
        "GAP_REPAIR",
        "DATASET_FREEZE",
        "OFFLINE_DEMO",
    ) or response["state"] not in (
        "PENDING",
        "CLAIMED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "DEAD",
        "CANCELED",
    ):
        raise DashboardApiError("control API response was invalid")
    for field in ("attempt_count", "max_attempts", "version"):
        value = response[field]
        if type(value) is not int or value < (0 if field == "attempt_count" else 1):
            raise DashboardApiError("control API response was invalid")
    if cast(int, response["attempt_count"]) > cast(int, response["max_attempts"]):
        raise DashboardApiError("control API response was invalid")
    for field in (
        "created_at",
        "updated_at",
        "next_attempt_at",
        "lease_expires_at",
        "completed_at",
    ):
        value = response[field]
        if value is None and field not in {"created_at", "updated_at"}:
            continue
        if type(value) is not str or not value.endswith("Z"):
            raise DashboardApiError("control API response was invalid")
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise DashboardApiError("control API response was invalid") from None
    for field in ("safe_last_error_code", "safe_last_error_message", "result_artifact_id"):
        value = response[field]
        if value is not None and (type(value) is not str or not value or len(value) > 256):
            raise DashboardApiError("control API response was invalid")
    artifact_id = response["result_artifact_id"]
    if artifact_id is not None and _ARTIFACT_ID.fullmatch(cast(str, artifact_id)) is None:
        raise DashboardApiError("control API response was invalid")


def _validated_base_url(value: object) -> str:
    if type(value) is not str or len(value) > 256 or not value.isascii() or "\\" in value:
        raise DashboardApiError("dashboard API origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise DashboardApiError("dashboard API origin is invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _ALLOWED_API_HOSTS
        or port != 8000
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise DashboardApiError("dashboard API origin is invalid")
    expected_netloc = "[::1]:8000" if parsed.hostname == "::1" else f"{parsed.hostname}:8000"
    if parsed.netloc != expected_netloc:
        raise DashboardApiError("dashboard API origin is invalid")
    return value


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _validate_route_response(
    route: DashboardRoute,
    response: dict[str, JsonValue],
) -> None:
    _validate_json_tree(response, depth=0)
    if route in {DashboardRoute.HEALTH_LIVE, DashboardRoute.HEALTH_READY}:
        _validate_health(route, response)
        return
    expected_fields = _PAGE_FIELDS.get(route)
    if expected_fields is None or frozenset(response) != {
        "count",
        "items",
        "limit",
        "offset",
    }:
        raise DashboardApiError("control API response was invalid")
    items = response.get("items")
    limit = response.get("limit")
    offset = response.get("offset")
    count = response.get("count")
    if (
        type(items) is not list
        or type(limit) is not int
        or not 1 <= limit <= 100
        or type(offset) is not int
        or offset < 0
        or type(count) is not int
        or count != len(items)
        or count > limit
    ):
        raise DashboardApiError("control API response was invalid")
    for item in items:
        if type(item) is not dict or frozenset(item) != expected_fields:
            raise DashboardApiError("control API response was invalid")
        _validate_item(route, item)


def _validate_health(route: DashboardRoute, response: dict[str, JsonValue]) -> None:
    if frozenset(response) != {"checked_at", "checks", "status"}:
        raise DashboardApiError("control API response was invalid")
    checked_at = response.get("checked_at")
    checks = response.get("checks")
    status = response.get("status")
    expected_statuses = {"live"} if route is DashboardRoute.HEALTH_LIVE else {"not_ready", "ready"}
    expected_checks = set() if route is DashboardRoute.HEALTH_LIVE else {"database"}
    if (
        type(checked_at) is not str
        or not checked_at.endswith("Z")
        or type(checks) is not dict
        or set(checks) != expected_checks
        or any(type(value) is not bool for value in checks.values())
        or type(status) is not str
        or status not in expected_statuses
    ):
        raise DashboardApiError("control API response was invalid")


def _validate_item(route: DashboardRoute, item: dict[str, JsonValue]) -> None:
    if route is DashboardRoute.SYSTEM_STATUS:
        if item != {
            "broker_authority": "none",
            "database": "reachable",
            "service": "control_api",
        }:
            raise DashboardApiError("control API response was invalid")
        return
    for field_name, value in item.items():
        if value is None:
            continue
        if field_name in _INTEGER_FIELDS:
            valid = type(value) is int and value >= 0
        elif field_name in _BOOLEAN_FIELDS:
            valid = type(value) is bool
        elif field_name in _JSON_CONTAINER_FIELDS:
            valid = type(value) in {dict, list}
        else:
            valid = type(value) is str and len(value) <= _MAX_TEXT_LENGTH
        if not valid:
            raise DashboardApiError("control API response was invalid")


def _validate_json_tree(value: object, *, depth: int, key: str | None = None) -> None:
    if depth > _MAX_NESTING_DEPTH or (key is not None and _is_sensitive_key(key)):
        raise DashboardApiError("control API response was invalid")
    if value is None or type(value) in {bool, int, float}:
        return
    if type(value) is str:
        if len(value) > _MAX_TEXT_LENGTH or _SECRET_TEXT.search(value) is not None:
            raise DashboardApiError("control API response was invalid")
        return
    if type(value) is list:
        items = cast(list[object], value)
        if len(items) > _MAX_CONTAINER_ITEMS:
            raise DashboardApiError("control API response was invalid")
        for item in items:
            _validate_json_tree(item, depth=depth + 1)
        return
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        if len(mapping) > _MAX_CONTAINER_ITEMS:
            raise DashboardApiError("control API response was invalid")
        for raw_key, item in mapping.items():
            if type(raw_key) is not str or not raw_key or len(raw_key) > 128:
                raise DashboardApiError("control API response was invalid")
            _validate_json_tree(item, depth=depth + 1, key=raw_key)
        return
    raise DashboardApiError("control API response was invalid")


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    compact = normalized.replace("_", "")
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS) or any(
        fragment in compact for fragment in _SENSITIVE_COMPACT_FRAGMENTS
    )
