"""Shared source/language router used by REST, MCP, and other product surfaces."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from isnad_core.hadith import HadeethEncClient, HadeethEncVerifier
from isnad_core.models import (
    MatchStatus,
    SourceMetadata,
    VerificationInput,
    VerificationResult,
)
from isnad_core.quran import QuranArabicVerifier, QuranEnglishVerifier


class VerifierAdapter(Protocol):
    """Minimal synchronous interface shared by source-specific verifier adapters."""

    @property
    def source_type(self) -> str: ...

    @property
    def language(self) -> str: ...

    @property
    def source_metadata(self) -> SourceMetadata: ...

    @property
    def normalization_profile(self) -> str: ...

    @property
    def mode(self) -> str: ...

    @property
    def reference_format(self) -> str: ...

    def verify(self, citation: VerificationInput) -> VerificationResult: ...


@dataclass(frozen=True, slots=True)
class VerificationCapability:
    """Stable descriptive contract for a configured source verifier."""

    source_type: str
    language: str
    source_metadata: SourceMetadata
    normalization_profile: str
    mode: str
    reference_format: str


class VerificationEngine:
    """Route citation inputs to the pinned source edition that can check them."""

    def __init__(self, adapters: Sequence[VerifierAdapter] | None = None) -> None:
        self._owned_resources: tuple[object, ...] = ()
        if adapters is None:
            hadeeth_client = HadeethEncClient()
            selected_adapters = (
                QuranArabicVerifier(),
                QuranEnglishVerifier(),
                HadeethEncVerifier("ar", hadeeth_client),
                HadeethEncVerifier("en", hadeeth_client),
            )
            self._owned_resources = (hadeeth_client,)
        else:
            selected_adapters = tuple(adapters)
        registry: dict[tuple[str, str], VerifierAdapter] = {}
        for adapter in selected_adapters:
            source_type = adapter.source_type.strip().casefold()
            language = adapter.language.strip().casefold()
            if not source_type or not language:
                raise ValueError("Verifier adapters must declare a source type and language.")
            key = (source_type, language)
            if key in registry:
                raise ValueError(f"Duplicate verifier adapter registration for {key}.")
            registry[key] = adapter
        if not registry:
            raise ValueError("At least one verifier adapter must be configured.")
        self._registry = MappingProxyType(registry)

    @property
    def capabilities(self) -> tuple[VerificationCapability, ...]:
        """Return a deterministic, immutable list of configured source editions."""

        return tuple(
            VerificationCapability(
                source_type=adapter.source_type,
                language=adapter.language,
                source_metadata=adapter.source_metadata,
                normalization_profile=adapter.normalization_profile,
                mode=adapter.mode,
                reference_format=adapter.reference_format,
            )
            for _, adapter in sorted(self._registry.items())
        )

    def verify(self, citation: VerificationInput) -> VerificationResult:
        """Verify a citation, or return an explicit unsupported-source outcome."""

        source_type = citation.source.strip().casefold()
        language = citation.language.strip().casefold()
        adapter = self._registry.get((source_type, language))
        if adapter is None:
            return VerificationResult(
                status=MatchStatus.UNSUPPORTED_SOURCE_OR_LANGUAGE,
                source_type=source_type,
                language=language,
                source_metadata=None,
                submitted_quote=citation.quote,
                cited_reference=citation.reference,
                matched_references=(),
                evidence=(),
                wording_differences=(),
                explanation=(
                    "No verifier is configured for this source and language. "
                    "This is not a finding that the citation is false."
                ),
            )
        return adapter.verify(citation)

    def close(self) -> None:
        """Release owned remote-source clients when the application shuts down."""

        for resource in self._owned_resources:
            close = getattr(resource, "close", None)
            if callable(close):
                close()
