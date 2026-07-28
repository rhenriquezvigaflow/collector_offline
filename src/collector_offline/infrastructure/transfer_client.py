from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import requests
from requests.adapters import HTTPAdapter

from collector_offline.config import CentralConfig, TransferConfig


class TransferTransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        category: str,
        quarantine: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.category = category
        self.quarantine = quarantine


@dataclass(frozen=True, slots=True)
class TransferStatus:
    transfer_id: UUID
    status: str
    confirmed_parts: tuple[int, ...] = ()
    missing_parts: tuple[int, ...] = ()
    staged_rows: int | None = None
    inserted_rows: int | None = None
    duplicate_rows: int | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class PartAcknowledgement:
    transfer_id: UUID
    part_number: int
    status: str
    confirmed_parts: tuple[int, ...] = ()


class TransferClient:
    def __init__(
        self,
        central: CentralConfig,
        transfer: TransferConfig,
        token: str,
    ) -> None:
        self.base_url = central.offline_transfer_url
        self.timeout = (
            transfer.connect_timeout_seconds,
            transfer.request_timeout_seconds,
        )
        self._verify: bool | str = central.ca_bundle or central.verify_tls
        self._session = requests.Session()
        self._session.mount(
            "https://",
            HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0, pool_block=True),
        )
        self._headers = {"Authorization": f"Bearer {token}"}

    def create_transfer(self, manifest: dict[str, Any]) -> TransferStatus:
        transfer_id = UUID(str(manifest["transfer_id"]))
        payload = self._request_json(
            "POST",
            "/transfers",
            expected={200, 201},
            json=manifest,
            quarantine_on_reject=True,
        )
        return self._parse_status(payload, transfer_id)

    def get_transfer(self, transfer_id: UUID) -> TransferStatus:
        payload = self._request_json(
            "GET",
            f"/transfers/{transfer_id}",
            expected={200},
        )
        return self._parse_status(payload, transfer_id)

    def upload_part(
        self,
        transfer_id: UUID,
        part_number: int,
        *,
        offset: int,
        part_sha256: str,
        data: bytes,
    ) -> PartAcknowledgement:
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
            "X-Part-Offset": str(offset),
            "X-Part-SHA256": part_sha256,
        }
        payload = self._request_json(
            "PUT",
            f"/transfers/{transfer_id}/parts/{part_number}",
            expected={200, 201},
            data=data,
            headers=headers,
            quarantine_on_reject=True,
        )
        try:
            response_transfer_id = UUID(str(payload["transfer_id"]))
            response_part = int(payload["part_number"])
            status = str(payload["status"]).lower()
            confirmed = self._response_part_numbers(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise TransferTransportError(
                f"invalid part acknowledgement: {exc}",
                retryable=True,
                category="invalid_response",
            ) from exc
        if response_transfer_id != transfer_id or response_part != part_number:
            raise TransferTransportError(
                "part acknowledgement identity does not match the request",
                retryable=True,
                category="invalid_response",
            )
        if status not in {"received", "already_received"}:
            raise TransferTransportError(
                f"invalid part acknowledgement status {status!r}",
                retryable=True,
                category="invalid_response",
            )
        return PartAcknowledgement(
            transfer_id=response_transfer_id,
            part_number=response_part,
            status=status,
            confirmed_parts=confirmed,
        )

    def complete_transfer(self, transfer_id: UUID) -> TransferStatus:
        payload = self._request_json(
            "POST",
            f"/transfers/{transfer_id}/complete",
            expected={200, 202},
        )
        return self._parse_status(payload, transfer_id)

    def get_status(self, transfer_id: UUID) -> TransferStatus:
        payload = self._request_json(
            "GET",
            f"/transfers/{transfer_id}",
            expected={200},
        )
        return self._parse_status(payload, transfer_id)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        expected: set[int],
        quarantine_on_reject: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        try:
            response = self._session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                timeout=self.timeout,
                verify=self._verify,
                **kwargs,
            )
        except requests.Timeout as exc:
            raise TransferTransportError(
                f"offline transfer timed out: {type(exc).__name__}",
                retryable=True,
                category="timeout",
            ) from exc
        except requests.RequestException as exc:
            raise TransferTransportError(
                f"offline transfer connectivity error: {type(exc).__name__}",
                retryable=True,
                category="connectivity",
            ) from exc

        if response.status_code not in expected:
            if response.status_code == 429 or response.status_code >= 500:
                raise TransferTransportError(
                    f"offline transfer returned retryable HTTP {response.status_code}",
                    retryable=True,
                    category="http_retryable",
                )
            if method == "POST" and path.endswith("/complete") and response.status_code == 409:
                raise TransferTransportError(
                    "central transfer still reports missing parts",
                    retryable=True,
                    category="incomplete_remote_transfer",
                )
            category = (
                "manifest_rejected"
                if method == "POST" and path == "/transfers"
                else "http_permanent"
            )
            if method == "PUT" and response.status_code in {409, 422}:
                category = "checksum_rejected"
            raise TransferTransportError(
                f"offline transfer returned permanent HTTP {response.status_code}",
                retryable=False,
                category=category,
                quarantine=quarantine_on_reject,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TransferTransportError(
                "offline transfer returned invalid JSON",
                retryable=True,
                category="invalid_response",
            ) from exc
        if not isinstance(payload, dict):
            raise TransferTransportError(
                "offline transfer response must be an object",
                retryable=True,
                category="invalid_response",
            )
        return payload

    @classmethod
    def _parse_status(
        cls,
        payload: dict[str, Any],
        expected_transfer_id: UUID,
    ) -> TransferStatus:
        try:
            transfer_id = UUID(str(payload["transfer_id"]))
            status = str(payload["status"]).upper()
            confirmed = cls._response_part_numbers(payload)
            missing = cls._part_numbers(payload.get("missing_parts", []))
        except (KeyError, TypeError, ValueError) as exc:
            raise TransferTransportError(
                f"invalid transfer status: {exc}",
                retryable=True,
                category="invalid_response",
            ) from exc
        if transfer_id != expected_transfer_id:
            raise TransferTransportError(
                "transfer status identity does not match the request",
                retryable=True,
                category="invalid_response",
            )
        return TransferStatus(
            transfer_id=transfer_id,
            status=status,
            confirmed_parts=confirmed,
            missing_parts=missing,
            staged_rows=cls._optional_int(payload.get("staged_rows")),
            inserted_rows=cls._optional_int(payload.get("inserted_rows")),
            duplicate_rows=cls._optional_int(payload.get("duplicate_rows")),
            last_error=str(payload["last_error"]) if payload.get("last_error") else None,
        )

    @staticmethod
    def _part_numbers(raw: Any) -> tuple[int, ...]:
        if not isinstance(raw, list):
            raise TypeError("part list is not an array")
        values = tuple(sorted({int(value) for value in raw}))
        if any(value < 1 for value in values):
            raise ValueError("part numbers must be one-based")
        return values

    @classmethod
    def _response_part_numbers(cls, payload: dict[str, Any]) -> tuple[int, ...]:
        aliases = [
            (name, cls._part_numbers(payload[name]))
            for name in ("confirmed_parts", "received_parts")
            if name in payload
        ]
        if not aliases:
            return ()
        if len(aliases) == 2 and aliases[0][1] != aliases[1][1]:
            raise ValueError("confirmed_parts and received_parts disagree")
        return aliases[0][1]

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        return None if value is None else int(value)

    def close(self) -> None:
        self._session.close()
