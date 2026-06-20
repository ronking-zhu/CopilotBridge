"""Layered request authentication for the direct chat + control API.

Selected by ``config.AUTH_MODE``:

* ``apikey`` (default) - the legacy shared ``CHAT_API_TOKEN`` (sent as
  ``X-API-Key``, ``Authorization: Bearer``, or a ``?key=`` query param). Behaviour
  is identical to before, so existing installs are unchanged.
* ``entra`` - **Microsoft Entra ID (Azure AD)** only. The client signs in
  interactively (microsoft.com + Authenticator MFA via Conditional Access) and
  sends the resulting access token as ``Authorization: Bearer <jwt>``. The server
  validates the token's signature (against the tenant's published keys),
  ``aud``/``iss``/``exp``, and that the user is allow-listed. The shared key is
  rejected. No static secret lives on the devices.
* ``both`` - accept either, for a phased migration.

The Entra path is **fail-closed**: if no allow rule (users/groups/roles) is
configured, every token is denied, so turning on ``entra`` without an allowlist
can never silently open the server to a whole tenant.

Validation uses PyJWT's ``PyJWKClient`` to fetch + cache the tenant signing keys;
nothing here ever needs a client secret.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("copilot_bridge.auth")

_AUTHORITY_HOST = "https://login.microsoftonline.com"
# Multi-tenant authorities can't pin a single issuer; we validate per-token tid.
_MULTITENANT = {"common", "organizations", "consumers"}


def _bearer(request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _looks_like_jwt(token: str) -> bool:
    # Three base64url segments => a JWT; a plain API key won't match.
    return token.count(".") == 2 and len(token) > 40


def _read_env_allowlists(env_path):
    """Read the three ``ENTRA_ALLOWED_*`` keys straight from a ``.env`` file.

    Returns ``(users, groups, roles)`` as lists. Used to hot-reload the allow-list
    in a running server when the Control Panel (a separate process) edits ``.env``,
    so adding a user never requires a restart. Absent keys come back empty.
    """
    users, groups, roles = [], [], []
    try:
        from pathlib import Path
        p = Path(env_path)
        if not p.is_file():
            return users, groups, roles
        keymap = {
            "ENTRA_ALLOWED_USERS": "users",
            "ENTRA_ALLOWED_GROUPS": "groups",
            "ENTRA_ALLOWED_ROLES": "roles",
        }
        bucket = {"users": [], "groups": [], "roles": []}
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            if k in keymap:
                bucket[keymap[k]] = [x.strip() for x in v.split(",") if x.strip()]
        return bucket["users"], bucket["groups"], bucket["roles"]
    except OSError:
        return users, groups, roles


class _EntraValidator:
    """Validates Microsoft Entra ID access tokens for this API."""

    def __init__(self, tenant_id, audiences, allowed_users, allowed_groups, allowed_roles,
                 env_path=None):
        self.tenant_id = (tenant_id or "").strip()
        self.audiences = [a for a in (audiences or []) if a]
        self.allowed_users = {u.strip().lower() for u in (allowed_users or []) if u.strip()}
        self.allowed_groups = {g.strip() for g in (allowed_groups or []) if g.strip()}
        self.allowed_roles = {r.strip() for r in (allowed_roles or []) if r.strip()}
        self._jwks_client = None
        # Hot-reload the allow-list from .env (edited by the Control Panel, a
        # separate process) keyed on file mtime, so adding a user takes effect
        # without restarting the server. Record the current mtime so we only
        # reload after a real change.
        self._env_path = env_path
        self._allow_mtime = None
        try:
            import os
            if env_path and os.path.isfile(env_path):
                self._allow_mtime = os.path.getmtime(env_path)
        except OSError:
            pass

    def _maybe_reload_allowlist(self) -> None:
        """Re-read ``ENTRA_ALLOWED_*`` from ``.env`` when the file changed."""
        if not self._env_path:
            return
        try:
            import os
            if not os.path.isfile(self._env_path):
                return
            mtime = os.path.getmtime(self._env_path)
        except OSError:
            return
        if mtime == self._allow_mtime:
            return
        self._allow_mtime = mtime
        users, groups, roles = _read_env_allowlists(self._env_path)
        self.allowed_users = {u.strip().lower() for u in users if u.strip()}
        self.allowed_groups = {g.strip() for g in groups if g.strip()}
        self.allowed_roles = {r.strip() for r in roles if r.strip()}
        logger.info("Reloaded Entra allow-list from .env: %d user(s), %d group(s), "
                    "%d role(s).", len(self.allowed_users), len(self.allowed_groups),
                    len(self.allowed_roles))

    @property
    def enabled(self) -> bool:
        return bool(self.tenant_id and self.audiences)

    @property
    def has_allowlist(self) -> bool:
        return bool(self.allowed_users or self.allowed_groups or self.allowed_roles)

    @property
    def _single_tenant(self) -> bool:
        return self.tenant_id.lower() not in _MULTITENANT

    def _jwks_uri(self) -> str:
        return f"{_AUTHORITY_HOST}/{self.tenant_id}/discovery/v2.0/keys"

    def _signing_key(self, token: str):
        """Return the RSA public key that signed ``token`` (cached by PyJWKClient).

        Isolated into a method so tests can inject a key without a network call.
        """
        import jwt
        if self._jwks_client is None:
            self._jwks_client = jwt.PyJWKClient(self._jwks_uri())
        return self._jwks_client.get_signing_key_from_jwt(token).key

    def validate(self, token: str):
        """Return ``(ok, identity, error)`` for an Entra access token."""
        import jwt

        # Pick up allow-list edits (Manage users) without a server restart.
        self._maybe_reload_allowlist()
        try:
            key = self._signing_key(token)
        except Exception as exc:  # noqa: BLE001 - network / key issues are non-fatal
            logger.warning("Entra JWKS/signing-key lookup failed: %s", exc)
            return False, None, "jwks-unreachable"

        decode_kw = dict(
            algorithms=["RS256"],
            audience=self.audiences,
            options={"require": ["exp", "iat", "aud", "iss"]},
            leeway=60,
        )
        if self._single_tenant:
            decode_kw["issuer"] = f"{_AUTHORITY_HOST}/{self.tenant_id}/v2.0"
        try:
            claims = jwt.decode(token, key, **decode_kw)
        except Exception as exc:  # noqa: BLE001 - invalid/expired/wrong-aud token
            logger.info("Entra token rejected: %s", exc)
            return False, None, "invalid-token"

        # Multi-tenant: the issuer must still match the token's own tenant.
        if not self._single_tenant:
            tid = claims.get("tid", "")
            expected = f"{_AUTHORITY_HOST}/{tid}/v2.0"
            if not tid or claims.get("iss") != expected:
                return False, None, "bad-issuer"

        identity = {
            "oid": claims.get("oid", ""),
            "tid": claims.get("tid", ""),
            "user": (claims.get("preferred_username") or claims.get("upn")
                     or claims.get("email") or claims.get("name") or ""),
            "name": claims.get("name", ""),
        }

        if not self.has_allowlist:
            logger.error("AUTH_MODE includes Entra but no ENTRA_ALLOWED_USERS/GROUPS/"
                         "ROLES is set; denying (fail-closed). Configure an allowlist.")
            return False, identity, "no-allowlist"

        if self._is_allowed(claims):
            return True, identity, ""
        logger.warning("Entra user %r not in the allowlist; denied.",
                       identity.get("user") or identity.get("oid"))
        return False, identity, "not-allowed"

    def _is_allowed(self, claims: dict) -> bool:
        if self.allowed_users:
            candidates = {
                str(claims.get("oid", "")).lower(),
                str(claims.get("preferred_username", "")).lower(),
                str(claims.get("upn", "")).lower(),
                str(claims.get("email", "")).lower(),
            }
            if self.allowed_users & {c for c in candidates if c}:
                return True
        if self.allowed_groups:
            if self.allowed_groups & set(claims.get("groups", []) or []):
                return True
        if self.allowed_roles:
            if self.allowed_roles & set(claims.get("roles", []) or []):
                return True
        return False


class Authenticator:
    """Decides whether a request may use the API, per ``config.AUTH_MODE``."""

    def __init__(self, config):
        self.mode = (getattr(config, "AUTH_MODE", "apikey") or "apikey").strip().lower()
        if self.mode not in ("apikey", "entra", "both"):
            logger.warning("Unknown AUTH_MODE %r; falling back to 'apikey'.", self.mode)
            self.mode = "apikey"
        self.api_token = getattr(config, "CHAT_API_TOKEN", "") or ""
        self.entra_client_id = getattr(config, "ENTRA_CLIENT_ID", "") or ""
        self.entra_scopes = list(getattr(config, "ENTRA_SCOPES", []) or [])
        try:
            from provisioning import DEFAULT_ENV_PATH as _env_path
        except Exception:  # noqa: BLE001
            _env_path = None
        self.entra = _EntraValidator(
            getattr(config, "ENTRA_TENANT_ID", ""),
            getattr(config, "ENTRA_AUDIENCE", []),
            getattr(config, "ENTRA_ALLOWED_USERS", []),
            getattr(config, "ENTRA_ALLOWED_GROUPS", []),
            getattr(config, "ENTRA_ALLOWED_ROLES", []),
            env_path=_env_path,
        )
        if self.mode in ("entra", "both") and not self.entra.enabled:
            logger.error("AUTH_MODE=%s but ENTRA_TENANT_ID/ENTRA_AUDIENCE are not set; "
                         "Entra sign-in will reject every request until configured.", self.mode)

    # -- key path (unchanged legacy semantics) ---------------------------

    def _check_api_key(self, request, allow_query: bool) -> bool:
        token = self.api_token
        if not token:
            return True  # no key configured => open (legacy behaviour)
        provided = request.headers.get("X-API-Key") or _bearer(request)
        if not provided and allow_query:
            provided = (request.query.get("key") or request.query.get("token")
                        or request.query.get("api_key"))
        return provided == token

    # -- public --------------------------------------------------------

    def check(self, request, allow_query: bool = False) -> bool:
        """True if the request is authorized. Stashes the verified Entra user (if
        any) on ``request['cb_user']`` for audit logging."""
        # Try Entra first when enabled and a JWT-looking token is present. Plain
        # <img>/download requests can't set an Authorization header, so when
        # ``allow_query`` is set we also accept the access token as a query param.
        if self.mode in ("entra", "both") and self.entra.enabled:
            token = _bearer(request)
            if not token and allow_query:
                token = (request.query.get("access_token")
                         or request.query.get("id_token") or "")
            if token and _looks_like_jwt(token):
                ok, identity, _err = self.entra.validate(token)
                if ok:
                    try:
                        request["cb_user"] = identity
                    except Exception:  # noqa: BLE001
                        pass
                    return True
                if self.mode == "entra":
                    return False  # key is disabled in entra-only mode
            elif self.mode == "entra":
                return False  # entra-only requires a bearer JWT

        if self.mode in ("apikey", "both"):
            return self._check_api_key(request, allow_query)
        return False

    def check_control(self, request) -> bool:
        """Authorize a Control-Panel request (``/api/control/*``).

        These are served on the same port as the public API, but the desktop
        Control Panel is a *local* helper that legitimately uses the host's own
        ``CHAT_API_TOKEN``. So accept the API key OR a valid Entra token regardless
        of AUTH_MODE, which keeps the Control Panel working while still blocking a
        remote caller who has neither. Falls back to open only when nothing is
        configured (legacy no-auth).
        """
        if self.api_token:
            provided = request.headers.get("X-API-Key") or _bearer(request)
            if provided and provided == self.api_token:
                return True
        if self.entra.enabled:
            token = _bearer(request)
            if token and _looks_like_jwt(token):
                ok, identity, _err = self.entra.validate(token)
                if ok:
                    try:
                        request["cb_user"] = identity
                    except Exception:  # noqa: BLE001
                        pass
                    return True
        # Nothing configured at all => legacy open behaviour.
        if not self.api_token and not (self.mode in ("entra", "both") and self.entra.enabled):
            return True
        return False

    def describe(self) -> dict:
        """Client-facing auth config for /api/webconfig (no secrets)."""
        info = {"authMode": self.mode, "authRequired": bool(self.api_token) or self.mode != "apikey"}
        if self.mode in ("entra", "both") and self.entra.enabled:
            authority = f"{_AUTHORITY_HOST}/{self.entra.tenant_id}"
            # Default the requested scope to the API's own access_as_user if the
            # admin didn't set ENTRA_SCOPES explicitly.
            scopes = self.entra_scopes
            if not scopes and self.entra.audiences:
                scopes = [f"{self.entra.audiences[0]}/access_as_user"]
            info["entra"] = {
                "tenantId": self.entra.tenant_id,
                "authority": authority,
                "clientId": self.entra_client_id,
                "audience": self.entra.audiences,
                "scopes": scopes,
            }
        return info
