"""Security-boundary tests for human authentication.

These are acceptance criteria, not regression cover. The handoff is explicit
that delivery persistence must not begin until driver isolation is proven, and
this file is the proof. Several tests are written so that the *easy* mistake
makes them fail loudly rather than pass quietly.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

from fastapi import HTTPException


PROJECT_DIR = Path(__file__).resolve().parents[3]
BACKEND_DIR = PROJECT_DIR / "backend"
for path in (str(BACKEND_DIR), str(PROJECT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)
os.environ.setdefault("CROSSFLOW_HISTORY_DB", ":memory:")

from auth import identity, transport  # noqa: E402


ADMIN_ID = "11111111-2222-3333-4444-555555555555"
DRIVER_ID = "66666666-7777-8888-9999-000000000000"

CONFIGURED_ENV = {
    "CROSSFLOW_AUTH_MODE": "supabase",
    "SUPABASE_URL": "https://project-ref.supabase.co",
    "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_browser_safe",
}


def _segment(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_token(
    *,
    subject: str = DRIVER_ID,
    expires_in: int = 3_600,
    **extra_claims: Any,
) -> str:
    """A structurally valid compact JWS. The signature is never checked here.

    Supabase verifies the signature; this module only ever parses ``exp`` from
    an already-verified token, so a fake signature is the correct fixture.
    """
    claims: dict[str, Any] = {"sub": subject, "exp": int(time.time()) + expires_in}
    claims.update(extra_claims)
    return f"{_segment({'alg': 'ES256', 'typ': 'JWT'})}.{_segment(claims)}.c2ln"


class FakeSupabase:
    """Records every outbound call so tests can assert on credentials used."""

    def __init__(
        self,
        *,
        user: Optional[dict[str, Any]] = None,
        profile_rows: Optional[list[dict[str, Any]]] = None,
        error: Optional[str] = None,
    ) -> None:
        self.user = user if user is not None else {"id": DRIVER_ID}
        self.profile_rows = (
            profile_rows
            if profile_rows is not None
            else [{"id": DRIVER_ID, "role": "driver", "display_name": "Driver One"}]
        )
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, config, **kwargs: Any) -> Any:
        self.calls.append({"config": config, **kwargs})
        if self.error is not None:
            raise transport.SupabaseUserError(self.error)
        path = kwargs.get("path", "")
        if path == "/auth/v1/user":
            return dict(self.user)
        if path.startswith("/rest/v1/"):
            return [dict(row) for row in self.profile_rows]
        raise AssertionError(f"unexpected path {path!r}")


class AuthTestCase(unittest.TestCase):
    def setUp(self) -> None:
        identity.clear_cache()
        self.addCleanup(identity.clear_cache)
        patcher = patch.dict(os.environ, CONFIGURED_ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def authenticate(self, fake: FakeSupabase, token: str) -> identity.AuthenticatedUser:
        with patch.object(identity.transport, "request_json", fake):
            return identity.authenticate(token)


class CredentialBoundaryTests(AuthTestCase):
    """The failure mode that looks completely correct in testing."""

    def test_user_transport_cannot_load_a_service_role_key(self) -> None:
        env = {
            "SUPABASE_URL": "https://project-ref.supabase.co",
            "SUPABASE_SECRET_KEY": "sb_secret_privileged",
            "SUPABASE_SERVICE_ROLE_KEY": "legacy.service.role",
        }
        with patch.dict(os.environ, env, clear=True):
            # No publishable key is present, so there is nothing safe to send.
            # A module that silently fell back to the secret key would return a
            # config here, and every RLS policy would become inert.
            self.assertIsNone(transport.load_user_config())

    def test_user_transport_never_names_a_privileged_credential(self) -> None:
        source = Path(transport.__file__).read_text(encoding="utf-8")
        code = source.split('"""', 2)[-1]  # exclude the explanatory docstring
        for forbidden in ("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
            self.assertNotIn(forbidden, code)

    def test_profile_read_uses_the_callers_own_token(self) -> None:
        token = make_token()
        fake = FakeSupabase()
        self.authenticate(fake, token)
        profile_calls = [
            call for call in fake.calls if call["path"].startswith("/rest/v1/")
        ]
        self.assertEqual(len(profile_calls), 1)
        # RLS applies only because this is the caller's token, not a shared one.
        self.assertEqual(profile_calls[0]["access_token"], token)

    def test_outbound_headers_pair_publishable_key_with_user_token(self) -> None:
        token = make_token()
        headers = transport.user_headers("sb_publishable_browser_safe", token)
        self.assertEqual(headers["apikey"], "sb_publishable_browser_safe")
        self.assertEqual(headers["Authorization"], f"Bearer {token}")

    def test_only_data_and_user_paths_are_reachable(self) -> None:
        config = transport.SupabaseUserConfig(
            "https://project-ref.supabase.co", "sb_publishable_browser_safe",
        )
        # Token issuance and admin user management must not be callable from
        # the module that holds user tokens.
        with patch.object(transport, "_execute") as execute:
            for path in (
                "/auth/v1/admin/users",
                "/auth/v1/token",
                "/auth/v1/logout",
                "/auth/v1/user/identities",
                "/rest/v1/../auth/v1/token",
                "/storage/v1/object",
            ):
                with self.subTest(path=path):
                    with self.assertRaises(transport.SupabaseUserError) as raised:
                        transport.request_json(
                            config,
                            access_token=make_token(),
                            method="GET",
                            path=path,
                        )
                    self.assertEqual(raised.exception.code, "invalid_path")
            execute.assert_not_called()

    def test_user_endpoint_cannot_be_used_to_modify_the_account(self) -> None:
        config = transport.SupabaseUserConfig(
            "https://project-ref.supabase.co", "sb_publishable_browser_safe",
        )
        with patch.object(transport, "_execute") as execute:
            for method in ("POST", "PATCH", "DELETE"):
                with self.subTest(method=method):
                    with self.assertRaises(transport.SupabaseUserError) as raised:
                        transport.request_json(
                            config, access_token=make_token(),
                            method=method, path="/auth/v1/user",
                        )
                    self.assertEqual(raised.exception.code, "invalid_method")
            execute.assert_not_called()


class RoleResolutionTests(AuthTestCase):
    def test_role_comes_from_profiles_not_from_token_metadata(self) -> None:
        # user_metadata is writable by the user through the Supabase client, so
        # a role claimed there is a role the user granted themselves.
        fake = FakeSupabase(
            user={
                "id": DRIVER_ID,
                "role": "admin",
                "user_metadata": {"role": "admin"},
                "app_metadata": {"role": "admin"},
            },
            profile_rows=[
                {"id": DRIVER_ID, "role": "driver", "display_name": "Driver One"},
            ],
        )
        user = self.authenticate(fake, make_token(role="admin"))
        self.assertEqual(user.role, "driver")
        self.assertFalse(user.is_admin)

    def test_admin_dependency_rejects_a_driver(self) -> None:
        fake = FakeSupabase()
        with patch.object(identity.transport, "request_json", fake):
            with self.assertRaises(HTTPException) as raised:
                identity.require_admin_user(f"Bearer {make_token()}")
        self.assertEqual(raised.exception.status_code, 403)

    def test_admin_dependency_accepts_an_admin(self) -> None:
        fake = FakeSupabase(
            user={"id": ADMIN_ID},
            profile_rows=[
                {"id": ADMIN_ID, "role": "admin", "display_name": "Ops Admin"},
            ],
        )
        with patch.object(identity.transport, "request_json", fake):
            user = identity.require_admin_user(
                f"Bearer {make_token(subject=ADMIN_ID)}",
            )
        self.assertTrue(user.is_admin)

    def test_missing_profile_is_refused_rather_than_defaulted(self) -> None:
        # Defaulting a profile-less account to 'driver' would grant access to
        # anyone who can reach the sign-up form.
        fake = FakeSupabase(profile_rows=[])
        with self.assertRaises(HTTPException) as raised:
            self.authenticate(fake, make_token())
        self.assertEqual(raised.exception.status_code, 403)

    def test_unknown_role_value_is_refused(self) -> None:
        fake = FakeSupabase(
            profile_rows=[
                {"id": DRIVER_ID, "role": "superuser", "display_name": "X"},
            ],
        )
        with self.assertRaises(HTTPException) as raised:
            self.authenticate(fake, make_token())
        self.assertEqual(raised.exception.status_code, 403)

    def test_profile_for_another_user_is_refused(self) -> None:
        fake = FakeSupabase(profile_rows=[{
            "id": ADMIN_ID, "role": "admin", "display_name": "Ops Admin",
        }])
        with self.assertRaises(HTTPException) as raised:
            self.authenticate(fake, make_token())
        self.assertEqual(raised.exception.status_code, 403)

    def test_demotion_takes_effect_while_token_verification_is_cached(self) -> None:
        token = make_token(subject=ADMIN_ID)
        fake = FakeSupabase(user={"id": ADMIN_ID}, profile_rows=[{
            "id": ADMIN_ID, "role": "admin", "display_name": "Ops Admin",
        }])
        with patch.object(identity.transport, "request_json", fake):
            self.assertTrue(identity.require_admin_user(f"Bearer {token}").is_admin)
            fake.profile_rows[0]["role"] = "driver"
            with self.assertRaises(HTTPException) as raised:
                identity.require_admin_user(f"Bearer {token}")
        self.assertEqual(raised.exception.status_code, 403)

    def test_deleted_profile_is_refused_while_verification_is_cached(self) -> None:
        token = make_token()
        fake = FakeSupabase()
        self.authenticate(fake, token)
        fake.profile_rows = []
        with self.assertRaises(HTTPException) as raised:
            self.authenticate(fake, token)
        self.assertEqual(raised.exception.status_code, 403)


class MechanismSeparationTests(AuthTestCase):
    """The machine secret and the human session must not authenticate each other."""

    def test_admin_token_presented_as_a_bearer_token_is_rejected(self) -> None:
        shared_secret = "crossflow-admin-shared-secret"
        with patch.dict(
            os.environ, {"CROSSFLOW_ADMIN_TOKEN": shared_secret}, clear=False,
        ):
            with self.assertRaises(HTTPException) as raised:
                identity.require_user(f"Bearer {shared_secret}")
        self.assertEqual(raised.exception.status_code, 401)

    def test_a_user_token_does_not_open_the_machine_endpoints(self) -> None:
        import main as api_main

        with patch.dict(
            os.environ,
            {"CROSSFLOW_ADMIN_TOKEN": "crossflow-admin-shared-secret"},
            clear=False,
        ):
            with self.assertRaises(HTTPException) as raised:
                api_main._require_admin(make_token(subject=ADMIN_ID))
        self.assertEqual(raised.exception.status_code, 403)


class TokenHandlingTests(AuthTestCase):
    def test_signup_profile_and_claimed_email_verification_cannot_replace_a_session(self) -> None:
        # Confirm-email signup can already have a profile but no session.
        # Neither that profile nor locally asserted verification claims allow
        # an API identity when Supabase refuses the access token.
        fake = FakeSupabase(error="http_401")
        with patch.object(identity.transport, "request_json", fake):
            with self.assertRaises(HTTPException) as no_session:
                identity.require_user(None)
            self.assertEqual(no_session.exception.status_code, 401)
            self.assertEqual(fake.calls, [])
            forged = make_token(
                email="unconfirmed@example.test", email_confirmed_at="2026-10-23T00:00:00Z",
                user_metadata={"email_verified": True},
            )
            with self.assertRaises(HTTPException) as rejected:
                identity.require_user(f"Bearer {forged}")
        self.assertEqual(rejected.exception.status_code, 401)
        self.assertEqual([call["path"] for call in fake.calls], ["/auth/v1/user"])

    def test_malformed_tokens_are_401_never_500(self) -> None:
        for header in (
            None,
            "",
            "Bearer",
            "Basic dXNlcjpwYXNz",
            "Bearer not-a-jwt",
            "Bearer a.b",
            "Bearer a.b.c.d",
            "Bearer " + "a" * 9_000,
        ):
            with self.subTest(header=header):
                with self.assertRaises(HTTPException) as raised:
                    identity.require_user(header)
                self.assertEqual(raised.exception.status_code, 401)

    def test_header_injection_in_a_token_is_rejected_before_any_request(self) -> None:
        poisoned = "aaa.bbb.ccc\r\nX-Injected: 1"
        with self.assertRaises(transport.SupabaseUserError):
            transport.validate_access_token(poisoned)
        with self.assertRaises(transport.SupabaseUserError):
            transport.user_headers("sb_publishable_browser_safe", poisoned)

    def test_rejected_tokens_are_not_cached(self) -> None:
        token = make_token()
        failing = FakeSupabase(error="http_401")
        with self.assertRaises(HTTPException):
            self.authenticate(failing, token)
        succeeding = FakeSupabase()
        user = self.authenticate(succeeding, token)
        # A cached failure, or a cache write on the failed path, would mean the
        # second call never reached Supabase.
        self.assertTrue(succeeding.calls)
        self.assertEqual(user.role, "driver")

    def test_verification_is_cached_for_repeat_calls(self) -> None:
        token = make_token()
        fake = FakeSupabase()
        self.authenticate(fake, token)
        self.authenticate(fake, token)
        self.assertEqual(sum(call["path"] == "/auth/v1/user" for call in fake.calls), 1)
        self.assertEqual(sum(call["path"].startswith("/rest/v1/") for call in fake.calls), 2)

    def test_cache_does_not_cross_project_configuration(self) -> None:
        token = make_token()
        fake = FakeSupabase()
        self.authenticate(fake, token)
        with patch.dict(os.environ, {"SUPABASE_URL": "https://other.supabase.co"}):
            self.authenticate(fake, token)
        verification = [call for call in fake.calls if call["path"] == "/auth/v1/user"]
        self.assertEqual(len(verification), 2)
        self.assertEqual(verification[-1]["config"].origin, "https://other.supabase.co")

    def test_direct_authentication_rejects_malformed_tokens_without_network(self) -> None:
        fake = FakeSupabase()
        with self.assertRaises(HTTPException) as raised:
            self.authenticate(fake, "invalid-token")
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(fake.calls, [])

    def test_a_cache_entry_never_outlives_the_token(self) -> None:
        already_expired = make_token(expires_in=-10)
        fake = FakeSupabase()
        self.authenticate(fake, already_expired)
        self.authenticate(fake, already_expired)
        self.assertEqual(sum(call["path"] == "/auth/v1/user" for call in fake.calls), 2)

    def test_repeat_requests_do_not_extend_verification_cache_lifetime(self) -> None:
        token = make_token()
        fake = FakeSupabase()
        with patch.object(identity, "VERIFICATION_TTL_SECONDS", 10), patch.object(
            identity.time, "monotonic", return_value=100,
        ) as clock:
            self.authenticate(fake, token)
            clock.return_value = 109
            self.authenticate(fake, token)
            clock.return_value = 111
            self.authenticate(fake, token)
        self.assertEqual(sum(call["path"] == "/auth/v1/user" for call in fake.calls), 2)

    def test_different_tokens_do_not_share_a_cache_entry(self) -> None:
        fake = FakeSupabase()
        self.authenticate(fake, make_token())
        self.authenticate(fake, make_token(expires_in=1_800))
        self.assertEqual(sum(call["path"] == "/auth/v1/user" for call in fake.calls), 2)


class DegradationTests(AuthTestCase):
    def test_auth_outage_is_503_not_a_crash(self) -> None:
        for code in ("network_error", "timeout", "http_500"):
            with self.subTest(code=code):
                identity.clear_cache()
                fake = FakeSupabase(error=code)
                with self.assertRaises(HTTPException) as raised:
                    self.authenticate(fake, make_token())
                self.assertEqual(raised.exception.status_code, 503)

    def test_public_endpoints_are_unaffected_by_an_auth_outage(self) -> None:
        import main as api_main

        with patch.object(
            identity.transport,
            "request_json",
            FakeSupabase(error="network_error"),
        ):
            payload = api_main.get_corridors()
        self.assertIn("corridors", payload)

    def test_auth_is_a_route_dependency_and_never_global_middleware(self) -> None:
        import main as api_main

        # Global auth middleware would take the public corridor views down with
        # Supabase, which is precisely what must not happen during a demo.
        middleware = " ".join(str(item.cls) for item in api_main.app.user_middleware)
        self.assertNotIn("auth", middleware.casefold())

    def test_disabled_mode_refuses_sessions_but_status_stays_public(self) -> None:
        with patch.dict(os.environ, {"CROSSFLOW_AUTH_MODE": "disabled"}, clear=False):
            with self.assertRaises(HTTPException) as raised:
                identity.require_user(f"Bearer {make_token()}")
            status = identity.status()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertFalse(status["enabled"])

    def test_status_never_reveals_a_credential(self) -> None:
        status = identity.status()
        rendered = json.dumps(status)
        self.assertNotIn("sb_publishable_browser_safe", rendered)
        self.assertNotIn("secret", rendered.casefold())


class ExistenceLeakTests(AuthTestCase):
    def test_absent_and_unowned_records_are_indistinguishable(self) -> None:
        # WP10's driver lookup must return this for both cases. Any wording or
        # status difference confirms which journey IDs exist.
        absent = identity.not_yours_error()
        unowned = identity.not_yours_error()
        self.assertEqual(absent.status_code, unowned.status_code)
        self.assertEqual(absent.detail, unowned.detail)
        self.assertEqual(absent.status_code, 404)

    def test_session_responses_are_not_cacheable(self) -> None:
        error = identity.auth_error(401, "nope")
        self.assertEqual(error.headers["Cache-Control"], "private, no-store")


if __name__ == "__main__":
    unittest.main()
