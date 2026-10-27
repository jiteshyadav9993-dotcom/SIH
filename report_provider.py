"""
report_provider.py
====================================================================
Boundary for the report-understanding model.

The live provider is not selected. Tests use MockReportProvider.
Credentials are read from the environment and are not written into errors.
====================================================================
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from report_schema import PROVIDER_RESPONSE_SCHEMA


class ProviderCallError(Exception):
    """The provider failed before it returned usable text."""


class ProviderTimeoutError(ProviderCallError):
    """The provider did not answer within the requested time."""


class ProviderNotConfiguredError(ProviderCallError):
    """A live adapter was requested, and none is configured."""


SCRIPTED_MOCK_LABEL = "SCRIPTED MOCK RESPONSE - NOT A LIVE AI RESULT"
FIXTURE_NOT_MATCHED_MESSAGE = (
    "No scripted fixture matched this document. The ordinary mock returned no findings."
)


@dataclass(frozen=True, slots=True)
class ImagePayload:
    """Bytes of one image that Stage 1 extracted from the uploaded file."""

    image_id: str
    media_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """One bounded chunk. Report text in the prompts is untrusted data."""

    chunk_id: str
    filename: str
    system_prompt: str
    user_prompt: str
    response_schema: dict[str, object]
    images: tuple[ImagePayload, ...]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """Raw model text. The pipeline parses and checks it again locally."""

    raw_text: str


class ReportUnderstandingProvider(Protocol):
    """One completion call. Implementations must not invent defect records."""

    name: str
    supports_images: bool

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Return the model text for this chunk, or raise ProviderCallError."""


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Environment settings. `public_view` omits the API key."""

    provider: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float

    def public_view(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "api_key_present": bool(self.api_key),
        }


class MockReportProvider:
    """Deterministic script. An empty script returns no findings and calls no network."""

    name = "mock"
    supports_images = False

    def __init__(
        self,
        script: Sequence[dict[str, object] | str | Exception] | None = None,
        *,
        supports_images: bool = False,
    ) -> None:
        self.supports_images = supports_images
        self._script: list[dict[str, object] | str | Exception] = list(script or [])
        self.requests: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if not self._script:
            return ProviderResponse(raw_text='{"summary": "", "findings": []}')
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return ProviderResponse(raw_text=item)
        import json

        return ProviderResponse(raw_text=json.dumps(item))


def provider_settings_from_env(environ: Mapping[str, str] | None = None) -> ProviderSettings:
    """Read REPORT_AI_* variables. The default provider name is mock."""
    source = os.environ if environ is None else environ
    raw_timeout = source.get("REPORT_AI_TIMEOUT_SECONDS", "30").strip() or "30"
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ProviderNotConfiguredError("REPORT_AI_TIMEOUT_SECONDS must be a number of seconds.") from exc
    if timeout <= 0:
        raise ProviderNotConfiguredError("REPORT_AI_TIMEOUT_SECONDS must be positive.")
    return ProviderSettings(
        provider=(source.get("REPORT_AI_PROVIDER", "mock").strip() or "mock"),
        base_url=source.get("REPORT_AI_BASE_URL", "").strip(),
        api_key=source.get("REPORT_AI_API_KEY", "").strip(),
        model=source.get("REPORT_AI_MODEL", "").strip(),
        timeout_seconds=timeout,
    )


def runtime_status(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """What the running process can actually do. The API key is not included."""
    from report_documents import TesseractOcrAdapter
    from report_nlp import PendingProfessorNlpAdapter

    settings = provider_settings_from_env(environ)
    source = os.environ if environ is None else environ
    fixture_name = source.get("REPORT_AI_MOCK_FIXTURE", "").strip()
    ocr_available = TesseractOcrAdapter().available()
    nlp_status = PendingProfessorNlpAdapter().status
    ocr_text = (
        "Tesseract OCR is available for scanned pages."
        if ocr_available
        else "Tesseract OCR is not available, so a scanned page or photograph cannot be read."
    )
    if settings.provider.casefold() != "mock":
        return {
            "provider": settings.provider,
            "live": False,
            "model": None,
            "scripted": False,
            "fixture": None,
            "label": None,
            "ocr_available": ocr_available,
            "nlp_status": nlp_status,
            "local_text": False,
            "limitation": (
                "No live report provider is configured. "
                f"Requested provider name: {settings.provider}. "
                "The pipeline will not invent findings. "
                f"{ocr_text} Professor NLP integration is {nlp_status}."
            ),
        }
    limitation = (
        "The runtime report provider is the deterministic mock. "
        "Ordinary uploads are read by local text rules when no scripted fixture matches. "
        "Those rules do not invent findings, summaries, severity values, or images, "
        "and they are not a live generative model. "
        "A live model adapter has not been configured or tested. "
        f"{ocr_text} Professor NLP integration is {nlp_status}."
    )
    if fixture_name:
        limitation += (
            f" Scripted fixture {fixture_name} is used only when the uploaded document matches it. "
            "A document that does not match is read by the local text rules. It is not a live model."
        )
    return {
        "provider": "mock",
        "live": False,
        "model": None,
        "scripted": False,
        "fixture": fixture_name or None,
        "label": SCRIPTED_MOCK_LABEL if fixture_name else None,
        "ocr_available": ocr_available,
        "nlp_status": nlp_status,
        "local_text": True,
        "limitation": limitation,
    }


def build_report_provider(environ: Mapping[str, str] | None = None) -> ReportUnderstandingProvider:
    """Return the mock provider, or refuse a live name that has no tested adapter."""
    settings = provider_settings_from_env(environ)
    if settings.provider.casefold() == "mock":
        return MockReportProvider()
    raise ProviderNotConfiguredError(
        "No live report provider is configured. "
        "The pipeline can use the mock provider. A live adapter must be added and tested "
        "before REPORT_AI_PROVIDER is set to another name. "
        f"Requested provider name: {settings.provider}."
    )


def empty_response_schema() -> dict[str, object]:
    return dict(PROVIDER_RESPONSE_SCHEMA)


__all__ = [
    "FIXTURE_NOT_MATCHED_MESSAGE",
    "ImagePayload",
    "MockReportProvider",
    "ProviderCallError",
    "ProviderNotConfiguredError",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderSettings",
    "ProviderTimeoutError",
    "ReportUnderstandingProvider",
    "SCRIPTED_MOCK_LABEL",
    "build_report_provider",
    "runtime_status",
    "empty_response_schema",
    "provider_settings_from_env",
]
