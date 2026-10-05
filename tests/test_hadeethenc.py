"""HadeethEnc matching preserves provenance and never guesses a hadith grade."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from isnad_core.errors import SourceUnavailable
from isnad_core.hadith import HadeethEncClient, HadeethEncVerifier, InvalidHadithReference
from isnad_core.models import MatchStatus, VerificationInput


def _record(
    record_id: str = "4560",
    *,
    text: str = "Narrated by Anas: A unique proverb is helpful, and kindness matters.",
    grade: str | None = "Authentic",
    attribution: str | None = "Agreed upon",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": record_id,
        "title": "A sample source record",
        "hadeeth": text,
        "attribution": attribution,
    }
    if grade is not None:
        record["grade"] = grade
    return record


def _verifier(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[HadeethEncVerifier, httpx.Client]:
    http_client = httpx.Client(
        base_url="https://hadeethenc.com/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    return HadeethEncVerifier("en", HadeethEncClient(http_client)), http_client


def test_cited_record_exact_match_keeps_grade_and_attribution_separate() -> None:
    source_record = _record()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/hadeeths/one/")
        assert request.url.params["id"] == "4560"
        return httpx.Response(200, json=source_record)

    verifier, http_client = _verifier(handler)
    try:
        result = verifier.verify(
            VerificationInput("hadith", "en", source_record["hadeeth"], "hadeethenc:4560")
        )
    finally:
        http_client.close()

    evidence = result.evidence[0]
    assert result.status is MatchStatus.EXACT_MATCH
    assert result.matched_references == ("hadeethenc:4560",)
    assert evidence.source_text == source_record["hadeeth"]
    assert evidence.record_title == "A sample source record"
    assert evidence.attribution_text == "Agreed upon"
    assert evidence.grade_text == "Authentic"
    assert evidence.grade_source == "HadeethEnc.com record 4560"
    assert evidence.graded_by is None
    assert evidence.bibliographic_reference is None
    assert result.source_metadata is not None
    assert "Curated selection" in result.source_metadata.coverage_note


def test_unreferenced_quote_search_returns_partial_source_evidence() -> None:
    source_record = _record()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/hadeeths/search/"):
            assert request.url.params["phrase"] == "kindness matters"
            return httpx.Response(200, json=[{"id": "4560"}])
        if request.url.path.endswith("/hadeeths/multiple/"):
            assert request.url.params["ids"] == "4560"
            return httpx.Response(200, json=[source_record])
        raise AssertionError(f"Unexpected endpoint {request.url.path}")

    verifier, http_client = _verifier(handler)
    try:
        result = verifier.verify(VerificationInput("hadith", "en", "kindness matters", None))
    finally:
        http_client.close()

    assert result.status is MatchStatus.PARTIAL_MATCH
    assert result.evidence[0].reference == "hadeethenc:4560"
    assert result.evidence[0].matched_fragment == "kindness matters"
    assert result.evidence[0].source_text == source_record["hadeeth"]


def test_missing_grade_is_not_inferred_from_attribution() -> None:
    source_record = _record(grade=None, attribution="Narrated by a named collection")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=source_record)

    verifier, http_client = _verifier(handler)
    try:
        result = verifier.verify(VerificationInput("hadith", "en", None, "hadeethenc:4560"))
    finally:
        http_client.close()

    evidence = result.evidence[0]
    assert result.status is MatchStatus.REFERENCE_FOUND_WITHOUT_QUOTE
    assert evidence.attribution_text == "Narrated by a named collection"
    assert evidence.grade_text is None
    assert evidence.grade_source is None
    assert evidence.graded_by is None


def test_no_search_result_is_limited_to_hadeethenc_and_not_a_fabrication_claim() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/hadeeths/search/")
        return httpx.Response(200, json=[])

    verifier, http_client = _verifier(handler)
    try:
        result = verifier.verify(
            VerificationInput("hadith", "en", "a wholly invented sample phrase", None)
        )
    finally:
        http_client.close()

    assert result.status is MatchStatus.NOT_FOUND_IN_CHECKED_CORPUS
    assert result.evidence == ()
    assert "fabricated or mawḍūʿ" in result.explanation


def test_truncated_search_with_no_candidates_does_not_claim_complete_absence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/hadeeths/search/"):
            return httpx.Response(200, json=[{"id": str(value)} for value in range(1, 12)])
        if request.url.path.endswith("/hadeeths/multiple/"):
            return httpx.Response(
                200,
                json=[
                    _record(str(value), text=f"Unrelated source wording {value}.")
                    for value in range(1, 11)
                ],
            )
        raise AssertionError(f"Unexpected endpoint {request.url.path}")

    verifier, http_client = _verifier(handler)
    try:
        result = verifier.verify(
            VerificationInput("hadith", "en", "a wholly invented sample phrase", None)
        )
    finally:
        http_client.close()

    assert result.status is MatchStatus.NOT_FOUND_IN_CHECKED_CORPUS
    assert result.evidence_truncated is True
    assert result.candidate_count is None
    assert "truncated HadeethEnc search" in result.explanation
    assert "fabricated or mawḍūʿ" in result.explanation


def test_remote_outage_raises_source_unavailable_instead_of_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic outage", request=request)

    verifier, http_client = _verifier(handler)
    try:
        with pytest.raises(SourceUnavailable):
            verifier.verify(VerificationInput("hadith", "en", "kindness matters", None))
    finally:
        http_client.close()


def test_wrong_record_reference_is_not_silently_reassigned() -> None:
    source_record = _record()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/hadeeths/one/"):
            return httpx.Response(404, json={})
        if request.url.path.endswith("/hadeeths/search/"):
            return httpx.Response(200, json=[{"id": "4560"}])
        if request.url.path.endswith("/hadeeths/multiple/"):
            return httpx.Response(200, json=[source_record])
        raise AssertionError(f"Unexpected endpoint {request.url.path}")

    verifier, http_client = _verifier(handler)
    try:
        result = verifier.verify(
            VerificationInput("hadith", "en", "kindness matters", "hadeethenc:999")
        )
    finally:
        http_client.close()

    assert result.status is MatchStatus.QUOTE_FOUND_WRONG_REFERENCE
    assert result.cited_reference == "hadeethenc:999"
    assert result.matched_references == ("hadeethenc:4560",)


def test_cited_record_mismatch_includes_source_evidence() -> None:
    source_record = _record()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/hadeeths/one/"):
            return httpx.Response(200, json=source_record)
        if request.url.path.endswith("/hadeeths/search/"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected endpoint {request.url.path}")

    verifier, http_client = _verifier(handler)
    try:
        result = verifier.verify(
            VerificationInput("hadith", "en", "not this wording", "hadeethenc:4560")
        )
    finally:
        http_client.close()

    assert result.status is MatchStatus.MISMATCH_AT_CITED_REFERENCE
    assert result.evidence[0].source_text == source_record["hadeeth"]
    assert result.wording_differences
    assert "authenticity" in result.explanation


def test_search_truncation_is_cached_and_never_claims_uniqueness() -> None:
    source_record = _record("1")
    search_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_requests
        if request.url.path.endswith("/hadeeths/search/"):
            search_requests += 1
            return httpx.Response(200, json=[{"id": str(value)} for value in range(1, 12)])
        if request.url.path.endswith("/hadeeths/multiple/"):
            records = [source_record] + [
                _record(str(value), text=f"Other record {value}.", grade=None, attribution=None)
                for value in range(2, 11)
            ]
            return httpx.Response(200, json=records)
        raise AssertionError(f"Unexpected endpoint {request.url.path}")

    verifier, http_client = _verifier(handler)
    try:
        first = verifier.verify(VerificationInput("hadith", "en", "kindness matters", None))
        second = verifier.verify(VerificationInput("hadith", "en", "kindness matters", None))
    finally:
        http_client.close()

    assert first.status is MatchStatus.AMBIGUOUS_MULTIPLE_MATCHES
    assert first.evidence_truncated is True
    assert first.candidate_count is None
    assert second.status is MatchStatus.AMBIGUOUS_MULTIPLE_MATCHES
    assert search_requests == 1


def test_invalid_hadeethenc_reference_is_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Malformed references must fail before network access")

    verifier, http_client = _verifier(handler)
    try:
        with pytest.raises(InvalidHadithReference):
            verifier.verify(VerificationInput("hadith", "en", None, "Muslim 1907"))
        with pytest.raises(InvalidHadithReference):
            verifier.verify(VerificationInput("hadith", "en", None, "hadeethenc:0"))
    finally:
        http_client.close()


def test_arabic_hadeethenc_text_uses_arabic_normalization() -> None:
    source_record = _record(
        text="عن عمر رضي الله عنه قال: إنما الأعمال بالنيات.",
        grade="صحيح",
        attribution="متفق عليه",
    )
    http_client = httpx.Client(
        base_url="https://hadeethenc.com/api/v1/",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=source_record)),
    )
    verifier = HadeethEncVerifier("ar", HadeethEncClient(http_client))
    try:
        result = verifier.verify(
            VerificationInput("hadith", "ar", "إنما الأعمال بالنيات", "hadeethenc:4560")
        )
    finally:
        http_client.close()

    assert result.status is MatchStatus.PARTIAL_MATCH
    assert result.evidence[0].grade_text == "صحيح"
    assert result.evidence[0].attribution_text == "متفق عليه"
