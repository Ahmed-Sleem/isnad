/* ============================================================================
   ISNAD CORE — citation block streamer.

   The model writes ordinary prose and wraps quotations in reserved markers:

     [[ISNAD-CITATION source=quran language=ar reference=2:255]]quote[[/ISNAD-CITATION]]

   Ordinary prose is released the moment it arrives; a marked block is held
   back entirely — quote and reference together — until the block is complete,
   so nothing unverified is ever painted into the transcript. A literal marker
   inside a quote is backslash-escaped by the model, and the escape is removed
   before the quote is sent for verification, matching the server-side gate.

   This file holds the whole protocol: no DOM, no network, no globals beyond the
   one export. tests/test_citation_stream_js.py exercises it directly under node,
   including markers split across chunk boundaries.
   ========================================================================= */
(function (root) {
  'use strict';

  var OPEN = '[[ISNAD-CITATION';
  var CLOSE = '[[/ISNAD-CITATION]]';
  var HEADER_LIMIT = 320;   /* a header longer than this is not a citation */
  var ATTRIBUTE = /^[A-Za-z_][A-Za-z0-9_]*$/;
  var VALUE = /^[^\s\]]+$/;

  function unescapeMarkers(text) {
    /* A single backslash before a reserved marker means the marker is literal
       text. A doubled backslash is a literal backslash in front of a marker. */
    return text
      .replace(/\\\[\[ISNAD-CITATION/g, OPEN)
      .replace(/\\\[\[\/ISNAD-CITATION\]\]/g, CLOSE)
      .replace(/\\\\/g, '\\');
  }

  function parseHeaders(headerText) {
    var headers = Object.create(null);
    var parts = headerText.trim().split(/\s+/);
    for (var i = 0; i < parts.length; i += 1) {
      if (!parts[i]) continue;
      var eq = parts[i].indexOf('=');
      if (eq <= 0) return null;
      var key = parts[i].slice(0, eq);
      var value = parts[i].slice(eq + 1);
      if (!ATTRIBUTE.test(key) || !VALUE.test(value)) return null;
      if (Object.prototype.hasOwnProperty.call(headers, key)) return null;
      headers[key] = value;
    }
    if (!headers.source || !headers.language) return null;
    return headers;
  }

  /* Longest suffix of `text` that is a proper prefix of `marker`. Used to hold
     back the tail of a chunk when a marker is split across chunks. */
  function partialMarkerLength(text, marker) {
    var max = Math.min(text.length, marker.length - 1);
    for (var size = max; size > 0; size -= 1) {
      if (text.slice(text.length - size) === marker.slice(0, size)) return size;
    }
    return 0;
  }

  /* Strip an escape that the split itself created: "...\" + "[[ISNAD-" must not
     be read as a literal marker just because the backslash arrived first. */
  function escapedAt(text, index) {
    var backslashes = 0;
    var cursor = index - 1;
    while (cursor >= 0 && text.charAt(cursor) === '\\') {
      backslashes += 1;
      cursor -= 1;
    }
    return backslashes % 2 === 1;
  }

  function createStreamer() {
    var pending = '';
    var blockText = '';
    var headerText = '';
    var state = 'prose';   /* prose | header | quote | discarded */
    var sequence = 0;

    function emitProse(piece) {
      return piece === '' ? [] : [{ type: 'prose', text: piece }];
    }

    /* Adjacent prose runs are one event: a marker split across chunks, or a
       discarded block between two prose runs, must not fragment the text the
       caller appends to the transcript. */
    function coalesce(events) {
      var merged = [];
      for (var i = 0; i < events.length; i += 1) {
        var previous = merged[merged.length - 1];
        if (events[i].type === 'prose' && previous && previous.type === 'prose') {
          previous.text += events[i].text;
          continue;
        }
        merged.push(events[i]);
      }
      return merged;
    }

    /* Screen out what the model wrote after an opening marker that turned out
       not to be a citation: a malformed header, or a header that never closes. */
    function discardBlock() {
      blockText = '';
      headerText = '';
      state = 'prose';
    }

    /* A block whose header was rejected is dropped through to its closing
       marker: the model wrote that text as a quotation, so echoing it back as
       prose would show unverified source text in the transcript. */
    function discardThroughClose() {
      blockText = '';
      headerText = '';
      state = 'discarded';
    }

    function beginBlock(header) {
      state = 'quote';
      blockText = '';
      sequence += 1;
      return [{ type: 'citation_start', id: sequence, header: header }];
    }

    /* The closing marker arrived: the held block is complete. */
    function finishBlock() {
      var event = {
        type: 'citation_end',
        id: sequence,
        quote: unescapeMarkers(blockText),
        withheld_characters: 0
      };
      discardBlock();
      return [event];
    }

    /* The model stopped mid-block. An unterminated quotation is never shown as
       a quotation: it is reported as held text that was not verified. */
    function abortBlock() {
      var event = {
        type: 'citation_incomplete',
        id: sequence,
        quote: '',
        withheld_characters: blockText.length
      };
      discardBlock();
      return [event];
    }

    function consume(text) {
      pending += text;
      var events = [];
      var guard = 0;

      while (pending.length && guard < 1000) {
        guard += 1;

        if (state === 'prose') {
          var openAt = pending.indexOf(OPEN);
          if (openAt === -1) {
            var hold = partialMarkerLength(pending, OPEN);
            events = events.concat(emitProse(pending.slice(0, pending.length - hold)));
            pending = hold ? pending.slice(pending.length - hold) : '';
            break;
          }
          if (escapedAt(pending, openAt)) {
            /* Literal marker text: release it as prose, unescaped. */
            events = events.concat(emitProse(unescapeMarkers(pending.slice(0, openAt + OPEN.length))));
            pending = pending.slice(openAt + OPEN.length);
            continue;
          }
          events = events.concat(emitProse(pending.slice(0, openAt)));
          pending = pending.slice(openAt + OPEN.length);
          state = 'header';
          headerText = '';
          continue;
        }

        if (state === 'header') {
          /* The header is scanned in one buffer, so a `]]` split across two
             chunks still closes it: holding only the tail of a marker would
             lose the boundary between the two pieces. */
          var headerBuffer = headerText + pending;
          var closeAt = headerBuffer.indexOf(']]');
          if (closeAt === -1) {
            if (headerBuffer.length > HEADER_LIMIT) {
              /* Not a citation: the "header" never ends. It is dropped rather
                 than echoed into the transcript, and the caller is told so the
                 reader is not left wondering where the sentence went. */
              pending = '';
              discardBlock();
              events.push({ type: 'citation_invalid', reason: 'oversized_header' });
              continue;
            }
            headerText = headerBuffer;
            pending = '';
            break;
          }
          var headers = parseHeaders(headerBuffer.slice(0, closeAt));
          pending = headerBuffer.slice(closeAt + 2);
          headerText = '';
          if (!headers) {
            discardThroughClose();
            events.push({ type: 'citation_invalid', reason: 'malformed_header' });
            continue;
          }
          events = events.concat(beginBlock(headers));
          continue;
        }

        if (state === 'discarded') {
          var endMarkerAt = pending.indexOf(CLOSE);
          if (endMarkerAt === -1) {
            var keepTail = partialMarkerLength(pending, CLOSE);
            pending = keepTail ? pending.slice(pending.length - keepTail) : '';
            break;
          }
          pending = pending.slice(endMarkerAt + CLOSE.length);
          state = 'prose';
          continue;
        }

        if (state === 'quote') {
          var endAt = pending.indexOf(CLOSE);
          if (endAt === -1) {
            var keep = partialMarkerLength(pending, CLOSE);
            blockText += pending.slice(0, pending.length - keep);
            pending = keep ? pending.slice(pending.length - keep) : '';
            break;
          }
          if (escapedAt(pending, endAt)) {
            /* The closing marker is literal text inside the quote. */
            blockText += pending.slice(0, endAt + CLOSE.length);
            pending = pending.slice(endAt + CLOSE.length);
            continue;
          }
          blockText += pending.slice(0, endAt);
          pending = pending.slice(endAt + CLOSE.length);
          events = events.concat(finishBlock());
          continue;
        }
      }

      return coalesce(events);
    }

    /* End of the model's output: an unterminated block is reported as
       incomplete instead of being shown as a quotation. */
    function finish() {
      var events = [];
      if (state === 'quote') {
        events = events.concat(abortBlock());
      } else if (state === 'header') {
        discardBlock();
        events.push({ type: 'citation_invalid', reason: 'unterminated_header' });
      } else if (state === 'discarded') {
        pending = '';
        state = 'prose';
      }
      events = events.concat(emitProse(pending));
      pending = '';
      return coalesce(events);
    }

    function hasOpenBlock() {
      return state === 'quote' || state === 'header';
    }

    return {
      push: consume,
      finish: finish,
      hasOpenBlock: hasOpenBlock,
      state: function () { return state; }
    };
  }

  var api = {
    createStreamer: createStreamer,
    unescapeMarkers: unescapeMarkers,
    parseHeaders: parseHeaders,
    OPEN_MARKER: OPEN,
    CLOSE_MARKER: CLOSE
  };

  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.IsnadCitationStream = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
