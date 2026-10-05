# Third-party data and source terms

The repository's MIT license applies to project software only; it does not relicense third-party source text.

## Qur'an Arabic text

The file `src/isnad_core/data/quran/tanzil-uthmani-v1.1.xml` is the Tanzil Project's Uthmani Quran text, version 1.1. It is reproduced verbatim from the official Tanzil download and retains the source copyright and license notice in its XML header.

- Source: [Tanzil Project](https://tanzil.net/)
- Download information: [Tanzil Quran Text](https://tanzil.net/download/)
- License stated by the source: Creative Commons Attribution 3.0
- Source terms: verbatim copies may be copied and distributed; the text must not be changed. Credit the Tanzil Project and link to `https://tanzil.net/` so users can follow updates.
- SHA-256: `8c5aeae20363a98f6963720d29fce040ca8b56a8e75f8b564c257fce7f6d0417`

Matching uses a separate, transient comparison key. The backend returns Qur'an text exactly as supplied by the selected source; it must not rewrite or replace that text.

## Qur'an English translation

The files under `src/isnad_core/data/quranenc/english_saheeh/` are unmodified JSON payloads fetched from QuranEnc's official English translation API. The edition is `english_saheeh`, version 1.1.2, titled “English Translation - Noor International Center.” The pinned manifest records the upstream edition metadata, publisher, version/update information, source URLs, per-file hashes, and the aggregate hash.

- Source and translation page: [QuranEnc English Saheeh](https://quranenc.com/en/browse/english_saheeh)
- API documentation: [QuranEnc API](https://quranenc.com/en/home/api/)
- Publisher: Noor International Center
- Reuse terms stated by QuranEnc: do not modify, add to, or delete content; clearly identify the publisher and QuranEnc.com; include the translation version and transcript/source information; notify QuranEnc about translation notes and keep the translation updated; do not include inappropriate advertisements when displaying the translation.

The pinned edition is bundled verbatim for offline matching. Check the official translation list for updates before changing the pin; review and validate a new edition before replacing this snapshot. The comparison profile ignores numeric footnote markers only as editorial annotations, while returned translation and footnote text remains unchanged.

## Hadith source

Hadith lookup uses the official [HadeethEnc API v1](https://hadeethenc.com/api-docs/) and is performed against HadeethEnc's curated selection; it is not comprehensive. The API data is fetched at request time and is not bundled as a local corpus.

- Source: [HadeethEnc.com](https://hadeethenc.com/en/home)
- Source terms: do not modify, add to, or delete content; clearly refer to the publisher and HadeethEnc.com. Consult the current [API documentation](https://hadeethenc.com/api-docs/) and [terms and policies](https://hadeethenc.com/en/home) before redistribution or deployment.

Hadith text, attribution, grade text, grade source, and named grader (if explicitly supplied) are separate fields. The API adapter does not infer a grader from an attribution. A report not located in this curated source is not labeled fabricated or mawḍūʿ.
