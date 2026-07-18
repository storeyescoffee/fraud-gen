"""Authenticates against the Storeyes panel API and caches the resulting JWT.

Login is only repeated when there is no cached token or the cached one is
past its expiry (minus a safety margin) — a fresh process reuses whatever
token is still on disk instead of re-authenticating on every start.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import requests

from src.config import GatewayConfig

logger = logging.getLogger(__name__)


class GatewayAuthError(Exception):
    """Raised when login against the gateway API fails."""


class GatewayClient:
    def __init__(self, config: GatewayConfig):
        self._config = config
        self._token_cache_path = config.token_cache_path
        self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._token: Optional[dict[str, Any]] = self._load_cached_token()

    def _load_cached_token(self) -> Optional[dict[str, Any]]:
        if not self._token_cache_path.is_file():
            return None
        try:
            data = json.loads(self._token_cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not {"access_token", "expires_at"} <= data.keys():
            return None
        return data

    def _save_token(self, token: dict[str, Any]) -> None:
        tmp_path = self._token_cache_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(token), encoding="utf-8")
        tmp_path.replace(self._token_cache_path)

    def _is_valid(self, token: Optional[dict[str, Any]]) -> bool:
        if not token:
            return False
        return time.time() < (token["expires_at"] - self._config.refresh_margin_seconds)

    def _login(self) -> dict[str, Any]:
        if not self._config.username or not self._config.password:
            raise GatewayAuthError("gateway.username and gateway.password must be set in config.conf")

        url = f"{self._config.base_url.rstrip('/')}{self._config.login_path}"
        logger.info(
            "Logging in to gateway",
            extra={"extra_fields": {"url": url, "username": self._config.username}},
        )
        try:
            response = requests.post(
                url,
                json={"username": self._config.username, "password": self._config.password},
                headers={"accept": "application/json, text/plain, */*"},
                timeout=30,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise GatewayAuthError(f"Gateway login failed: {exc}") from exc
        except ValueError as exc:
            raise GatewayAuthError(f"Gateway login returned a non-JSON response: {exc}") from exc

        try:
            access_token = body["accessToken"]
            expires_in = int(body["expiresIn"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayAuthError(f"Gateway login response missing expected fields: {exc}") from exc

        token = {
            "access_token": access_token,
            "refresh_token": body.get("refreshToken"),
            "token_type": body.get("tokenType", "Bearer"),
            "expires_at": time.time() + expires_in,
        }
        self._save_token(token)
        self._token = token
        logger.info("Gateway login succeeded", extra={"extra_fields": {"expires_in": expires_in}})
        return token

    def get_access_token(self) -> str:
        """Return a valid access token, logging in (or re-logging in) as needed."""
        if not self._is_valid(self._token):
            self._token = self._login()
        return self._token["access_token"]

    def auth_header(self) -> dict[str, str]:
        access_token = self.get_access_token()
        token_type = self._token.get("token_type", "Bearer")
        return {"Authorization": f"{token_type} {access_token}"}

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Authenticated request helper for future gateway API calls.

        Retries once after a fresh login if the server reports the token as
        no longer valid (401), in case it expired earlier than our local
        expiry estimate.
        """
        url = path if path.startswith("http") else f"{self._config.base_url.rstrip('/')}{path}"
        timeout = kwargs.pop("timeout", 30)
        extra_headers = kwargs.pop("headers", {})

        headers = {**extra_headers, **self.auth_header()}
        response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)

        if response.status_code == 401:
            logger.warning("Gateway returned 401, forcing re-login and retrying once")
            self._token = self._login()
            headers = {**extra_headers, **self.auth_header()}
            response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)

        return response
