"""Load and validate the pinned, verbatim Tanzil Uthmani corpus at process startup."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType

from isnad_core.models import SourceMetadata

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_PINNED_TANZIL_SHA256 = "8c5aeae20363a98f6963720d29fce040ca8b56a8e75f8b564c257fce7f6d0417"
_REFERENCE_PATTERN = re.compile(
    r"^(?P<surah>[0-9]{1,3}):(?P<start>[0-9]{1,3})(?:-(?P<end>[0-9]{1,3}))?$"
)


class QuranCorpusError(RuntimeError):
    """Raised when the pinned corpus or its manifest is invalid."""


class InvalidQuranReference(ValueError):
    """Raised for malformed or out-of-corpus Qur'an references."""


@dataclass(frozen=True, slots=True)
class QuranReference:
    """A validated ayah or same-surah ayah range."""

    surah: int
    start_ayah: int
    end_ayah: int

    @property
    def display(self) -> str:
        """Render a canonical reference using ASCII digits."""

        if self.start_ayah == self.end_ayah:
            return f"{self.surah}:{self.start_ayah}"
        return f"{self.surah}:{self.start_ayah}-{self.end_ayah}"

    @classmethod
    def parse(cls, value: str) -> QuranReference:
        """Parse a user-supplied reference without guessing its meaning."""

        candidate = value.translate(_ARABIC_DIGITS).strip().replace("–", "-").replace("—", "-")
        candidate = re.sub(r"\s+", "", candidate)
        match = _REFERENCE_PATTERN.fullmatch(candidate)
        if match is None:
            raise InvalidQuranReference("Expected a reference such as 2:255 or 2:255-257.")
        surah = int(match.group("surah"))
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if surah < 1 or start < 1:
            raise InvalidQuranReference("Surah and ayah numbers must start at 1.")
        if end < start:
            raise InvalidQuranReference("The ending ayah must not precede the starting ayah.")
        return cls(surah, start, end)


@dataclass(frozen=True, slots=True)
class QuranVerse:
    """One source ayah and any edition-supplied explanatory footnotes."""

    surah: int
    ayah: int
    surah_name: str
    text: str
    footnotes: str | None = None

    @property
    def reference(self) -> str:
        """Return this ayah's canonical locator."""

        return f"{self.surah}:{self.ayah}"


@dataclass(frozen=True, slots=True)
class QuranCorpus:
    """Validated immutable view of the pinned Arabic source text."""

    verses: tuple[QuranVerse, ...]
    verses_by_reference: Mapping[str, QuranVerse]
    verses_by_surah: Mapping[int, tuple[QuranVerse, ...]]
    source: SourceMetadata

    @classmethod
    def from_verses(
        cls,
        verses: Sequence[QuranVerse],
        source: SourceMetadata,
    ) -> QuranCorpus:
        """Build an immutable edition from complete, contiguous source ayahs."""

        frozen_verses = tuple(verses)
        if len(frozen_verses) != 6_236:
            raise QuranCorpusError("A complete Qur'an edition must contain 6,236 ayahs.")
        by_surah: dict[int, list[QuranVerse]] = {}
        for verse in frozen_verses:
            if not 1 <= verse.surah <= 114 or not verse.surah_name or not verse.text:
                raise QuranCorpusError("A source ayah has invalid metadata or empty text.")
            surah_verses = by_surah.setdefault(verse.surah, [])
            if verse.ayah != len(surah_verses) + 1:
                raise QuranCorpusError(f"Surah {verse.surah} has non-contiguous ayah numbers.")
            surah_verses.append(verse)
        if set(by_surah) != set(range(1, 115)):
            raise QuranCorpusError("A complete Qur'an edition must include all 114 surahs.")

        by_reference = MappingProxyType({verse.reference: verse for verse in frozen_verses})
        frozen_by_surah = MappingProxyType(
            {number: tuple(items) for number, items in by_surah.items()}
        )
        return cls(frozen_verses, by_reference, frozen_by_surah, source)

    @classmethod
    def load_default(cls) -> QuranCorpus:
        """Load the packaged Tanzil source after checking its pinned checksum."""

        package_root = files("isnad_core")
        manifest_path = package_root.joinpath("data", "quran", "manifest.json")
        corpus_path = package_root.joinpath("data", "quran", "tanzil-uthmani-v1.1.xml")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw_xml = corpus_path.read_bytes()
        except (OSError, json.JSONDecodeError) as exc:
            raise QuranCorpusError(
                "The packaged Qur'an corpus or manifest could not be read."
            ) from exc

        if not isinstance(manifest, dict):
            raise QuranCorpusError("The Qur'an corpus manifest must be a JSON object.")
        expected_hash = manifest.get("sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise QuranCorpusError("The Qur'an corpus manifest has no valid SHA-256 pin.")
        if expected_hash != _PINNED_TANZIL_SHA256:
            raise QuranCorpusError("The Qur'an corpus manifest does not match the build-time pin.")
        source_values: dict[str, str] = {}
        for field in ("source", "source_url", "version", "license"):
            value = manifest.get(field)
            if not isinstance(value, str) or not value.strip():
                raise QuranCorpusError(f"The Qur'an corpus manifest has no valid {field}.")
            source_values[field] = value
        actual_hash = hashlib.sha256(raw_xml).hexdigest()
        if actual_hash != _PINNED_TANZIL_SHA256:
            raise QuranCorpusError("The Qur'an corpus checksum does not match the build-time pin.")

        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError as exc:
            raise QuranCorpusError("The pinned Qur'an XML could not be parsed.") from exc
        if root.tag != "quran":
            raise QuranCorpusError("The pinned Qur'an XML has an unexpected root element.")

        verses: list[QuranVerse] = []
        by_surah: dict[int, list[QuranVerse]] = {}
        for surah_node in root.findall("sura"):
            try:
                surah_number = int(surah_node.attrib["index"])
                surah_name = surah_node.attrib["name"]
            except (KeyError, ValueError) as exc:
                raise QuranCorpusError("A surah entry is missing its index or name.") from exc
            if not 1 <= surah_number <= 114 or not surah_name:
                raise QuranCorpusError("A surah entry has an invalid index or empty name.")
            if surah_number in by_surah:
                raise QuranCorpusError(f"Duplicate surah index {surah_number} in the corpus.")

            surah_verses: list[QuranVerse] = []
            expected_ayah = 1
            for ayah_node in surah_node.findall("aya"):
                try:
                    ayah_number = int(ayah_node.attrib["index"])
                    text = ayah_node.attrib["text"]
                except (KeyError, ValueError) as exc:
                    raise QuranCorpusError("An ayah entry is missing its index or text.") from exc
                if ayah_number != expected_ayah or not text:
                    raise QuranCorpusError(
                        f"Surah {surah_number} has a missing, repeated, or empty ayah "
                        f"at {expected_ayah}."
                    )
                verse = QuranVerse(surah_number, ayah_number, surah_name, text)
                surah_verses.append(verse)
                verses.append(verse)
                expected_ayah += 1
            if not surah_verses:
                raise QuranCorpusError(f"Surah {surah_number} contains no ayahs.")
            by_surah[surah_number] = surah_verses

        if len(by_surah) != manifest.get("surah_count"):
            raise QuranCorpusError("Surah count does not match the pinned manifest.")
        if len(verses) != manifest.get("ayah_count"):
            raise QuranCorpusError("Ayah count does not match the pinned manifest.")
        if set(by_surah) != set(range(1, 115)):
            raise QuranCorpusError(
                "The pinned corpus does not contain each surah index exactly once."
            )

        source = SourceMetadata(
            source_id="tanzil-quran-uthmani",
            name=source_values["source"],
            url=source_values["source_url"],
            version=source_values["version"],
            license=source_values["license"],
            content_sha256=actual_hash,
        )
        return cls.from_verses(verses, source)

    def resolve(self, reference: QuranReference) -> tuple[QuranVerse, ...]:
        """Return all ayahs in a validated reference or raise a typed error."""

        if not 1 <= reference.surah <= 114:
            raise InvalidQuranReference("The surah number must be between 1 and 114.")
        if reference.start_ayah < 1 or reference.end_ayah < reference.start_ayah:
            raise InvalidQuranReference("Ayah numbers must be positive and ranges must be ordered.")
        surah_verses = self.verses_by_surah[reference.surah]
        if reference.end_ayah > len(surah_verses):
            raise InvalidQuranReference(f"Surah {reference.surah} has {len(surah_verses)} ayahs.")
        return surah_verses[reference.start_ayah - 1 : reference.end_ayah]

    @staticmethod
    def reference_for(surah: int, ayah: int) -> str:
        """Format a canonical source locator."""

        return f"{surah}:{ayah}"
