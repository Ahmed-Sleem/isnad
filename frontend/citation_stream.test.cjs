/* Node test for the citation streamer. Run with:
     node frontend/citation_stream.test.cjs
   tests/test_citation_stream_js.py invokes this file from pytest. */
'use strict';

const assert = require('assert');
const { createStreamer, unescapeMarkers, OPEN_MARKER, CLOSE_MARKER } =
  require('./citation_stream.js');

function eventsFor(chunks) {
  const streamer = createStreamer();
  let events = [];
  chunks.forEach((chunk) => { events = events.concat(streamer.push(chunk)); });
  return events.concat(streamer.finish());
}

function types(events) {
  return events.map((event) => event.type);
}

const block = (source, language, reference, quote) =>
  `${OPEN_MARKER} source=${source} language=${language} reference=${reference}]]${quote}${CLOSE_MARKER}`;

/* 1. Prose around a block: prose first, then the block, then prose. */
{
  const events = eventsFor([`Before. ${block('quran', 'ar', '2:255', 'quote text')} After.`]);
  assert.deepStrictEqual(types(events), ['prose', 'citation_start', 'citation_end', 'prose']);
  assert.strictEqual(events[0].text, 'Before. ');
  assert.strictEqual(events[3].text, ' After.');
  const header = events[1].header;
  assert.deepStrictEqual(
    { source: header.source, language: header.language, reference: header.reference },
    { source: 'quran', language: 'ar', reference: '2:255' }
  );
  assert.strictEqual(events[2].quote, 'quote text');
}

/* 2. A quote is withheld until the block closes - never released early. */
{
  const streamer = createStreamer();
  const first = streamer.push(`${OPEN_MARKER} source=hadith language=en reference=hadeethenc:1]]half`);
  assert.deepStrictEqual(types(first), ['citation_start']);
  assert.strictEqual(streamer.hasOpenBlock(), true);
  const second = streamer.push(` the quote${CLOSE_MARKER}`);
  assert.deepStrictEqual(types(second), ['citation_end']);
  assert.strictEqual(second[0].quote, 'half the quote');
  assert.strictEqual(streamer.hasOpenBlock(), false);
}

/* 3. Markers split across arbitrary chunk boundaries. */
{
  const text = `Intro ${block('quran', 'en', '112:1', 'Say, He is Allah')} outro`;
  for (let size = 1; size <= 7; size += 1) {
    const chunks = [];
    for (let i = 0; i < text.length; i += size) chunks.push(text.slice(i, i + size));
    const events = eventsFor(chunks);
    assert.deepStrictEqual(
      types(events).filter((type) => type !== 'prose'),
      ['citation_start', 'citation_end'],
      `chunk size ${size}`
    );
    const quote = events.find((event) => event.type === 'citation_end').quote;
    assert.strictEqual(quote, 'Say, He is Allah', `chunk size ${size}`);
    const joined = events.filter((event) => event.type === 'prose').map((event) => event.text).join('');
    assert.strictEqual(joined, 'Intro  outro', `chunk size ${size}`);
  }
}

/* 4. An escaped closing marker inside a quote is literal text. */
{
  const quote = `a ${'\\' + CLOSE_MARKER} b`;
  const events = eventsFor([block('quran', 'en', '1:1', quote)]);
  assert.deepStrictEqual(types(events), ['citation_start', 'citation_end']);
  assert.strictEqual(events[1].quote, `a ${CLOSE_MARKER} b`);
}

/* 5. An escaped opening marker in prose is released as plain text. */
{
  const events = eventsFor([`Code sample: ${'\\' + OPEN_MARKER}]] not a citation`]);
  assert.deepStrictEqual(types(events), ['prose']);
  assert.strictEqual(events[0].text, `Code sample: ${OPEN_MARKER}]] not a citation`);
}

/* 6. A malformed header is rejected without echoing the marker text. */
{
  const events = eventsFor([`Text ${OPEN_MARKER} source=quran]]oops${CLOSE_MARKER} after`]);
  assert.strictEqual(events.some((event) => event.type === 'citation_invalid'), true);
  const text = events.filter((event) => event.type === 'prose').map((event) => event.text).join('');
  assert.strictEqual(text, 'Text  after');
  assert.strictEqual(text.includes(OPEN_MARKER), false);
  assert.strictEqual(text.includes('oops'), false);
}

/* 7. A block left open at the end is reported, not rendered as a quote. */
{
  const streamer = createStreamer();
  streamer.push(`${OPEN_MARKER} source=quran language=ar reference=1:1]]unfinished`);
  const events = streamer.finish();
  assert.deepStrictEqual(types(events), ['citation_incomplete']);
  assert.strictEqual(events[0].quote, '');
  assert.strictEqual(events[0].withheld_characters, 'unfinished'.length);
}

/* 8. Duplicate or empty attributes are rejected. */
{
  assert.strictEqual(eventsFor([`${OPEN_MARKER} source=quran language=ar language=en]]x${CLOSE_MARKER}`])
    .some((event) => event.type === 'citation_invalid'), true);
  assert.strictEqual(eventsFor([`${OPEN_MARKER} source= language=ar]]x${CLOSE_MARKER}`])
    .some((event) => event.type === 'citation_invalid'), true);
  assert.strictEqual(eventsFor([`${OPEN_MARKER} source=quran language=ar]]x${CLOSE_MARKER}`])
    .some((event) => event.type === 'citation_invalid'), false, 'reference is optional');
}

/* 9. Two blocks in one chunk keep distinct ids and ordered output. */
{
  const events = eventsFor([
    `${block('quran', 'ar', '1:1', 'first')} middle ${block('hadith', 'ar', 'hadeethenc:9', 'second')}`
  ]);
  assert.deepStrictEqual(types(events), [
    'citation_start', 'citation_end', 'prose', 'citation_start', 'citation_end'
  ]);
  assert.strictEqual(events[1].id, 1);
  assert.strictEqual(events[4].id, 2);
  assert.strictEqual(events[4].quote, 'second');
}

/* 10. An unterminated "header" is dropped rather than flooding the transcript. */
{
  const events = eventsFor([`Start ${OPEN_MARKER} ${'x'.repeat(400)}`]);
  assert.strictEqual(events.some((event) => event.type === 'citation_invalid'), true);
  const text = events.filter((event) => event.type === 'prose').map((event) => event.text).join('');
  assert.strictEqual(text, 'Start ');
}

/* 11. The unescape helper leaves ordinary backslashes alone. */
assert.strictEqual(unescapeMarkers('a \\ b'), 'a \\ b');
assert.strictEqual(unescapeMarkers('saw \\' + OPEN_MARKER), `saw ${OPEN_MARKER}`);

console.log('citation_stream: all assertions passed');
