/* ============================================================================
   ISNAD CORE — chat interface with source-checked quotations.

   Two surfaces, one shell:
   - Chat: the user talks to a model. The citation protocol prompt is injected
     automatically as the system message, the model's prose streams straight
     through, and every marked quotation is held, verified against the pinned
     source and rendered as a citation card.
   - Verify: the same verification applied by hand, for a quote with no model
     involved.

   Rules this file exists to enforce:
   - Nothing unverified is ever shown as a quotation. A marked block is held
     whole (quote and reference together) until the verifier answers.
   - The model's own words for a quotation are never displayed: the card shows
     the source's wording, or the API's failure, and nothing in between.
   - A match status is never presented as a hadith grade, an authenticity
     judgement, or a ruling. A grade appears only when the source supplies one.
   - The model API key is a secret: kept in memory by default, stored on the
     device only if the user explicitly asks, never logged, never sent anywhere
     except the model endpoint the user configured.
   ========================================================================= */
(function () {
  'use strict';

  /* ==========================================================================
     1. UTILITIES
     ======================================================================= */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));
  const uid = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  function icon(id, cls) {
    return '<svg aria-hidden="true"' + (cls ? ' class="' + cls + '"' : '') +
      '><use href="#' + id + '"/></svg>';
  }

  function formatTime(ts) {
    try {
      return new Date(ts).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    } catch (e) { return ''; }
  }

  function formatDuration(ms) {
    if (typeof ms !== 'number' || !isFinite(ms) || ms < 0) return '';
    return ms >= 1000 ? (ms / 1000).toFixed(1) + ' s' : Math.max(1, Math.round(ms)) + ' ms';
  }

  function formatCount(n) {
    return typeof n === 'number' && isFinite(n) ? n.toLocaleString() : '';
  }

  function shortHash(value) {
    const text = String(value || '');
    return text.length > 12 ? text.slice(0, 12) + '…' : text;
  }

  function humanizeToken(value) {
    const text = String(value == null ? '' : value).replace(/_/g, ' ').trim();
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : '';
  }

  const storage = {
    get(key, fallback) {
      try {
        const raw = window.localStorage.getItem(key);
        return raw == null ? fallback : JSON.parse(raw);
      } catch (e) { return fallback; }
    },
    set(key, value) {
      try { window.localStorage.setItem(key, JSON.stringify(value)); return true; }
      catch (e) { return false; }
    },
    remove(key) { try { window.localStorage.removeItem(key); } catch (e) { /* ignore */ } }
  };

  /* ==========================================================================
     2. MARKDOWN — escape first, then a small safe subset.
     ======================================================================= */
  const MD = (function () {

    function inline(src) {
      const codes = [];
      let s = src;
      s = s.replace(/`([^`]+)`/g, function (_, code) {
        codes.push(code);
        return '\u0000C' + (codes.length - 1) + '\u0000';
      });
      s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      s = s.replace(/(^|[^*\w])\*([^*\n]+)\*/g, '$1<em>$2</em>');
      s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
      s = s.replace(
        /\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer nofollow">$1</a>'
      );
      s = s.replace(/\u0000C(\d+)\u0000/g, function (_, i) {
        return '<code class="inline-code">' + codes[Number(i)] + '</code>';
      });
      return s;
    }

    function codeBlock(escapedCode, lang) {
      const safeLang = escapeHtml(lang || 'text');
      return '<div class="codeblock">' +
        '<div class="codeblock__bar">' +
          '<span class="codeblock__lang">' + safeLang + '</span>' +
          '<button class="codeblock__copy" type="button" data-copy-code>Copy</button>' +
        '</div>' +
        '<pre class="codeblock__pre" tabindex="0" role="region" aria-label="' +
          safeLang + ' sample, scrollable"><code>' + escapedCode + '</code></pre>' +
      '</div>';
    }

    /* Returns one HTML string per top-level block, so a streaming message can
       repaint only the block that is still being written. */
    function renderBlocks(source) {
      const lines = escapeHtml(source == null ? '' : source).split('\n');
      const blocks = [];
      let html = '';
      let i = 0;
      let listType = null;
      let inCode = false;
      let codeLang = '';
      let codeBuf = [];

      function flush() { if (html !== '') { blocks.push(html); html = ''; } }
      function closeList() { if (listType) { html += '</' + listType + '>'; listType = null; flush(); } }

      while (i < lines.length) {
        const line = lines[i];

        const fence = line.match(/^```(\w*)\s*$/);
        if (fence) {
          if (!inCode) { closeList(); inCode = true; codeLang = fence[1] || 'text'; codeBuf = []; }
          else { inCode = false; html += codeBlock(codeBuf.join('\n'), codeLang); flush(); }
          i += 1;
          continue;
        }
        if (inCode) { codeBuf.push(line); i += 1; continue; }

        const heading = line.match(/^(#{1,4})\s+(.*)$/);
        if (heading) {
          closeList();
          const level = Math.min(heading[1].length + 2, 6);
          html += '<h' + level + '>' + inline(heading[2]) + '</h' + level + '>';
          flush();
          i += 1;
          continue;
        }

        if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line.trim())) {
          closeList(); html += '<hr>'; flush(); i += 1; continue;
        }

        if (/^&gt;\s?/.test(line)) {
          closeList();
          const buf = [];
          while (i < lines.length && /^&gt;\s?/.test(lines[i])) {
            buf.push(lines[i].replace(/^&gt;\s?/, ''));
            i += 1;
          }
          html += '<blockquote>' + renderBlocks(unescapeNested(buf.join('\n'))).join('') + '</blockquote>';
          flush();
          continue;
        }

        const ul = line.match(/^\s*[-*]\s+(.*)$/);
        if (ul) {
          if (listType !== 'ul') { closeList(); html += '<ul>'; listType = 'ul'; }
          html += '<li>' + inline(ul[1]) + '</li>';
          i += 1;
          continue;
        }

        const ol = line.match(/^\s*\d+\.\s+(.*)$/);
        if (ol) {
          if (listType !== 'ol') { closeList(); html += '<ol>'; listType = 'ol'; }
          html += '<li>' + inline(ol[1]) + '</li>';
          i += 1;
          continue;
        }

        if (line.trim() === '') { closeList(); i += 1; continue; }

        closeList();
        const buf = [line];
        i += 1;
        while (
          i < lines.length &&
          lines[i].trim() !== '' &&
          !/^(#{1,4}\s|```|\s*[-*]\s|\s*\d+\.\s|&gt;)/.test(lines[i])
        ) {
          buf.push(lines[i]);
          i += 1;
        }
        html += '<p>' + inline(buf.join(' ')) + '</p>';
        flush();
      }

      if (inCode) { html += codeBlock(codeBuf.join('\n'), codeLang); flush(); }
      closeList();
      flush();
      return blocks;
    }

    function unescapeNested(s) {
      return s
        .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
    }

    return { render: (source) => renderBlocks(source).join(''), renderBlocks: renderBlocks, escape: escapeHtml };
  })();

  /* The transcript paints block by block and reuses finished blocks, so a
     streaming answer costs one block per token, not a re-parse of everything. */
  const Rich = (function () {
    const states = new WeakMap();

    function paint(container, source) {
      if (!container) return 0;
      const blocks = MD.renderBlocks(source || '');
      let state = states.get(container);
      if (!state) { state = { html: [], nodes: [] }; states.set(container, state); }

      while (state.nodes.length > blocks.length) {
        const gone = state.nodes.pop();
        state.html.pop();
        if (gone && gone.parentNode === container) container.removeChild(gone);
      }

      for (let i = 0; i < blocks.length; i += 1) {
        const previous = state.nodes[i];
        if (state.html[i] === blocks[i] && previous && previous.parentNode === container) continue;

        const holder = document.createElement('div');
        holder.innerHTML = blocks[i];
        const node = holder.firstElementChild;
        if (!node) continue;

        if (previous && previous.parentNode === container) container.replaceChild(node, previous);
        else container.appendChild(node);

        state.nodes[i] = node;
        state.html[i] = blocks[i];
      }
      return blocks.length;
    }

    return { paint: paint };
  })();

  /* ==========================================================================
     3. TOASTS
     ======================================================================= */
  const Toasts = (function () {
    const root = $('#toasts');
    const active = new Map();

    function dismiss(el) {
      el.setAttribute('data-leaving', 'true');
      window.setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 180);
    }

    function show(message, tone) {
      const key = (tone || '') + '|' + message;
      const existing = active.get(key);
      if (existing) {
        window.clearTimeout(existing.timer);
        existing.timer = window.setTimeout(function () { active.delete(key); dismiss(existing.el); }, 3200);
        return;
      }
      const el = document.createElement('div');
      el.className = 'toast' + (tone ? ' toast--' + tone : '');
      el.setAttribute('role', 'status');
      el.innerHTML = (tone === 'error' ? icon('ic-warn') : icon('ic-check')) +
        '<span>' + escapeHtml(message) + '</span>';
      root.appendChild(el);
      const entry = { el: el, timer: 0 };
      entry.timer = window.setTimeout(function () { active.delete(key); dismiss(el); }, 3200);
      active.set(key, entry);
    }

    return { show: show };
  })();

  /* ==========================================================================
     4. VERIFICATION API CLIENT
     ======================================================================= */
  const Api = (function () {

    function normalizeBase(value) {
      const raw = String(value == null ? '' : value).trim();
      if (!raw) return '';
      let parsed;
      try { parsed = new URL(raw); } catch (e) { throw new Error('not-a-url'); }
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') throw new Error('bad-scheme');
      if (parsed.username || parsed.password) throw new Error('credentials-in-url');
      if (parsed.search || parsed.hash) throw new Error('not-a-base');
      return parsed.origin + parsed.pathname.replace(/\/+$/, '');
    }

    function base() { return Store.state.settings.apiBase || ''; }
    function url(path) { return base() + path; }
    function describeBase() {
      if (base()) return base();
      return window.location.protocol === 'file:' ? 'this page’s origin' : window.location.origin;
    }

    async function request(path, options) {
      const opts = options || {};
      const controller = new AbortController();
      const onAbort = function () { controller.abort(); };
      if (opts.signal) {
        if (opts.signal.aborted) controller.abort();
        else opts.signal.addEventListener('abort', onAbort, { once: true });
      }
      const timer = window.setTimeout(function () { controller.abort('timeout'); }, opts.timeoutMs || 15000);
      const started = Date.now();

      let response;
      try {
        response = await window.fetch(url(path), {
          method: opts.method || 'GET',
          headers: Object.assign(
            { Accept: 'application/json' },
            opts.body ? { 'Content-Type': 'application/json' } : null
          ),
          body: opts.body ? JSON.stringify(opts.body) : undefined,
          signal: controller.signal,
          cache: 'no-store',
          credentials: 'omit',
          mode: 'cors'
        });
      } catch (err) {
        const aborted = controller.signal.aborted;
        const external = opts.signal && opts.signal.aborted;
        const reason = controller.signal.reason;
        if (aborted && !external && reason === 'timeout') {
          throw apiError('timeout', 'The request to ' + describeBase() + ' did not finish in time.');
        }
        if (external || aborted) throw apiError('cancelled', 'Request cancelled.');
        throw apiError('network', 'Could not reach the API at ' + describeBase() + '.');
      } finally {
        window.clearTimeout(timer);
        if (opts.signal) opts.signal.removeEventListener('abort', onAbort);
      }

      const text = await response.text();
      let payload = null;
      if (text) { try { payload = JSON.parse(text); } catch (e) { payload = null; } }

      if (!response.ok) {
        const body = payload && payload.error ? payload.error : {};
        throw apiError(body.code || ('http_' + response.status), body.message || response.statusText, {
          httpStatus: response.status,
          requestId: payload && payload.request_id,
          retryAfter: response.headers.get('Retry-After'),
          details: body.details || [],
          durationMs: Date.now() - started
        });
      }
      if (!payload || typeof payload !== 'object') {
        throw apiError('invalid_response', 'The API returned a body that is not JSON.');
      }
      return { payload: payload, durationMs: Date.now() - started, status: response.status };
    }

    function apiError(code, message, extra) {
      const err = new Error(message);
      err.isnad = Object.assign({ code: code, message: message }, extra || {});
      return err;
    }

    return {
      normalizeBase: normalizeBase,
      base: base,
      describeBase: describeBase,
      capabilities: (options) => request('/v1/capabilities', options),
      ready: (options) => request('/health/ready', options),
      systemPrompt: (options) => request('/v1/system-prompt', options),
      verify: (body, options) => request('/v1/verify', Object.assign({ method: 'POST', body: body, timeoutMs: 45000 }, options)),
      error: apiError
    };
  })();

  /* ==========================================================================
     5. MODEL CLIENT — any OpenAI-compatible chat completions endpoint.

     Streaming only: the citation gate needs to see tokens as they arrive, and a
     non-streamed answer would arrive with its quotations already displayed.
     ======================================================================= */
  const Model = (function () {

    function normalizeBase(value) {
      const raw = String(value == null ? '' : value).trim();
      if (!raw) return '';
      let parsed;
      try { parsed = new URL(raw); } catch (e) { throw new Error('not-a-url'); }
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') throw new Error('bad-scheme');
      if (parsed.username || parsed.password) throw new Error('credentials-in-url');
      if (parsed.search || parsed.hash) throw new Error('not-a-base');
      const path = parsed.pathname.replace(/\/+$/, '');
      return parsed.origin + (path || '/v1');
    }

    function configured() {
      const settings = Store.state.settings;
      return Boolean(settings.modelBase && settings.modelName);
    }

    function endpoint() {
      return Store.state.settings.modelBase.replace(/\/+$/, '') + '/chat/completions';
    }

    function label() {
      const settings = Store.state.settings;
      return settings.modelName || 'no model selected';
    }

    /* Reads an OpenAI-style SSE stream. Chunks can split anywhere, so a buffer
       keeps the tail until a newline arrives. */
    async function streamChat(messages, options) {
      const opts = options || {};
      const settings = Store.state.settings;
      const providerKey = Store.modelKey();

      const headers = {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream'
      };
      if (providerKey) headers.Authorization = 'Bearer ' + providerKey;
      if (settings.modelReferer) headers['HTTP-Referer'] = settings.modelReferer;
      if (settings.modelTitle) headers['X-Title'] = settings.modelTitle;

      const controller = new AbortController();
      const onAbort = function () { controller.abort(); };
      if (opts.signal) {
        if (opts.signal.aborted) controller.abort();
        else opts.signal.addEventListener('abort', onAbort, { once: true });
      }

      let response;
      try {
        response = await window.fetch(endpoint(), {
          method: 'POST',
          headers: headers,
          body: JSON.stringify({
            model: settings.modelName,
            messages: messages,
            stream: true,
            temperature: settings.temperature
          }),
          signal: controller.signal,
          cache: 'no-store',
          credentials: 'omit'
        });
      } catch (err) {
        if (controller.signal.aborted) throw modelError('cancelled', 'Generation cancelled.');
        throw modelError('network',
          'Could not reach the model endpoint at ' + settings.modelBase + '.');
      }

      if (!response.ok) {
        let detail = '';
        try {
          const body = await response.text();
          detail = body.slice(0, 400);
        } catch (e) { /* the status alone will have to do */ }
        throw modelError('http_' + response.status,
          'The model endpoint refused the request (HTTP ' + response.status + ').',
          { httpStatus: response.status, detail: detail });
      }
      if (!response.body) throw modelError('no_stream', 'The model endpoint returned no stream.');

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      try {
        for (;;) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });

          let newline = buffer.indexOf('\n');
          while (newline !== -1) {
            const line = buffer.slice(0, newline).trim();
            buffer = buffer.slice(newline + 1);
            newline = buffer.indexOf('\n');
            if (!line.startsWith('data:')) continue;
            const data = line.slice(5).trim();
            if (data === '[DONE]') { opts.onDone && opts.onDone(); return; }
            let parsed;
            try { parsed = JSON.parse(data); } catch (e) { continue; }
            const choice = parsed && parsed.choices && parsed.choices[0];
            const delta = choice && (choice.delta || choice.message);
            const text = delta && typeof delta.content === 'string' ? delta.content : '';
            if (text) opts.onText && opts.onText(text);
            if (parsed && parsed.error) {
              throw modelError('provider_error',
                String(parsed.error.message || 'The provider reported an error.'));
            }
          }
        }
        opts.onDone && opts.onDone();
      } finally {
        if (opts.signal) opts.signal.removeEventListener('abort', onAbort);
        try { reader.releaseLock(); } catch (e) { /* already released */ }
      }
    }

    function modelError(code, message, extra) {
      const err = new Error(message);
      err.isnad = Object.assign({ code: code, message: message }, extra || {});
      return err;
    }

    return {
      normalizeBase: normalizeBase,
      configured: configured,
      label: label,
      endpoint: endpoint,
      streamChat: streamChat
    };
  })();

  /* ==========================================================================
     6. STATUS TABLE AND ERROR COPY

     Nine textual-correspondence statuses. None is an authenticity grade, and
     the copy says so rather than leaving room to assume one.
     ======================================================================= */
  const STATUS_TABLE = {
    exact_match: {
      label: 'Exact match', tone: 'positive',
      meaning: 'The submitted text matches the source wording at the cited reference.'
    },
    normalized_match: {
      label: 'Match after normalization', tone: 'positive',
      meaning: 'The text matches the source once non-lexical differences such as diacritics and tatweel are set aside by the source’s normalization profile.'
    },
    partial_match: {
      label: 'Partial match', tone: 'caution',
      meaning: 'Only part of the submitted text corresponds to the source. The quote is not fully supported as submitted.'
    },
    mismatch_at_cited_reference: {
      label: 'Mismatch at the cited reference', tone: 'negative',
      meaning: 'The cited reference does not contain the submitted wording.'
    },
    quote_found_wrong_reference: {
      label: 'Quote found at a different reference', tone: 'caution',
      meaning: 'The wording was located in the source, but not at the reference that was cited.'
    },
    reference_found_without_quote: {
      label: 'Reference found, no quote compared', tone: 'info',
      meaning: 'The reference exists in the checked source. No quoted text was submitted, so no wording was compared.'
    },
    not_found_in_checked_corpus: {
      label: 'Not found in the checked corpus', tone: 'negative',
      meaning: 'The bounded source that was searched does not contain this wording. This says nothing about sources outside that corpus, and nothing about whether the report is authentic or fabricated.'
    },
    ambiguous_multiple_matches: {
      label: 'Ambiguous — multiple matches', tone: 'caution',
      meaning: 'More than one location in the source matched. The result cannot be reduced to a single reference.'
    },
    unsupported_source_or_language: {
      label: 'Source or language not supported', tone: 'info',
      meaning: 'No adapter is configured for this source and language pair.'
    }
  };

  function statusInfo(status) {
    return STATUS_TABLE[status] || {
      label: String(status || 'Unknown status'), tone: 'info',
      meaning: 'This build does not recognize the returned status. Treat the raw value with caution and check the API version.'
    };
  }

  function hasNoApiOrigin() {
    return !Api.base() && window.location.protocol === 'file:';
  }

  function describeError(err) {
    const e = (err && err.isnad) || { code: 'unknown', message: String(err && err.message || err) };
    const base = Api.describeBase();

    switch (e.code) {
      case 'cancelled':
        return { title: 'Cancelled', tone: 'cancelled', message: 'Cancelled before a result was returned.' };
      case 'timeout':
        return { title: 'The API timed out', tone: 'error', message: 'No response arrived from ' + base + ' in time. Retrying is safe; nothing was decided about the citation.' };
      case 'network':
        if (hasNoApiOrigin()) {
          return {
            title: 'No API configured', tone: 'error',
            message: 'This interface was opened from disk, so there is no API origin to call. Open Connection settings and enter the API base URL.'
          };
        }
        return { title: 'API unreachable', tone: 'error', message: 'Could not reach ' + base + '. Check that the service is running, that the base URL is correct, and that this page’s origin is listed in ISNAD_CORS_ORIGINS.' };
      case 'source_unavailable':
        return {
          title: 'Source unavailable', tone: 'error',
          message: 'The upstream source could not be reached, so nothing was checked.' +
            (e.retryAfter ? ' The API asked to retry after ' + e.retryAfter + ' seconds.' : ''),
          detail: 'This is a source failure, not a not-found result. The quote was not verified.'
        };
      case 'validation_error':
      case 'invalid_reference':
      case 'unsupported_source':
      case 'unsupported_language':
        return {
          title: 'Request rejected', tone: 'error',
          message: e.message || 'The API rejected this request as invalid.',
          detail: e.details && e.details.length
            ? 'Fields: ' + e.details.map(function (d) {
                return (d.location || []).join('.') + (d.code ? ' (' + d.code + ')' : '');
              }).join(', ')
            : ''
        };
      case 'rate_limited':
        return { title: 'Rate limited', tone: 'error', message: 'The API is limiting requests.' + (e.retryAfter ? ' Retry after ' + e.retryAfter + ' seconds.' : '') };
      case 'invalid_response':
        return { title: 'Unreadable response', tone: 'error', message: 'The API replied with a body that is not valid JSON, so no result could be read.' };
      default:
        return {
          title: 'Verification failed', tone: 'error',
          message: e.message || 'The verification request did not complete.',
          detail: [e.httpStatus ? 'HTTP ' + e.httpStatus : '', e.code ? 'code: ' + e.code : '']
            .filter(Boolean).join(' · ')
        };
    }
  }

  function describeModelError(err) {
    const e = (err && err.isnad) || { code: 'unknown', message: String(err && err.message || err) };
    const settings = Store.state.settings;
    switch (e.code) {
      case 'cancelled':
        return { title: 'Stopped', tone: 'cancelled', message: 'Generation was stopped.' };
      case 'network':
        return {
          title: 'Model unreachable', tone: 'error',
          message: 'Could not reach ' + (settings.modelBase || 'the model endpoint') + '. ' +
            'Check the base URL, the network, and whether the provider allows browser requests from this origin ' +
            '(a provider that blocks CORS will need a local proxy such as LiteLLM, documented in integrations.md).'
        };
      case 'http_401':
      case 'http_403':
        return { title: 'Model rejected the key', tone: 'error', message: 'The endpoint refused the API key (HTTP ' + e.httpStatus + '). Re-enter it in Model settings.' };
      case 'http_429':
        return { title: 'Model rate limited', tone: 'error', message: 'The provider is rate limiting this key. Wait, then try again.' };
      case 'no_stream':
        return { title: 'No stream', tone: 'error', message: 'The endpoint answered without a streamed body, so no citation could be checked.' };
      case 'provider_error':
        return { title: 'Provider error', tone: 'error', message: e.message };
      default:
        return {
          title: 'Model request failed', tone: 'error',
          message: e.message || 'The model request did not complete.',
          detail: [e.httpStatus ? 'HTTP ' + e.httpStatus : '', e.detail || ''].filter(Boolean).join(' — ')
        };
    }
  }

  /* ==========================================================================
     7. STORE
     ======================================================================= */
  const Store = (function () {

    const LIMITS = { chats: 40, items: 120 };

    const state = {
      chats: [],
      activeId: null,
      query: '',
      mode: 'chat',
      theme: document.documentElement.getAttribute('data-theme') || 'light',
      settings: {
        apiBase: '',
        modelBase: '',
        modelName: '',
        modelRememberKey: false,
        temperature: 0.3,
        direction: 'ltr',
        showTimestamps: true
      },
      running: { active: false, controller: null },
      capabilities: null,
      ready: null,
      systemPrompt: null,
      apiState: 'checking'
    };

    /* The key lives here and nowhere else unless the user opts in. It is never
       written into a conversation, an export, or a log line. */
    let modelKey = '';

    const listeners = [];
    function emit() {
      listeners.forEach(function (fn) {
        try { fn(state); } catch (e) { /* one broken listener must not stop the rest */ }
      });
    }
    function subscribe(fn) {
      listeners.push(fn);
      return function () { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); };
    }

    function chat(id) { return state.chats.find(function (c) { return c.id === id; }) || null; }
    function active() { return chat(state.activeId); }
    function item(chatId, itemId) {
      const record = chat(chatId);
      return record ? (record.items.find(function (m) { return m.id === itemId; }) || null) : null;
    }

    function createChat() {
      const record = {
        id: uid(), title: 'New chat', items: [], createdAt: Date.now(), updatedAt: Date.now()
      };
      state.chats.unshift(record);
      state.activeId = record.id;
      emit();
      return record;
    }

    function selectChat(id) { if (chat(id)) { state.activeId = id; emit(); } }

    function deleteChat(id) {
      const idx = state.chats.findIndex(function (c) { return c.id === id; });
      if (idx < 0) return;
      state.chats.splice(idx, 1);
      if (state.activeId === id) state.activeId = state.chats.length ? state.chats[0].id : null;
      if (!state.chats.length) createChat(); else emit();
    }

    function renameChat(id, title) {
      const record = chat(id);
      if (!record) return;
      const next = String(title || '').trim();
      if (!next) return;
      record.title = next;
      record.updatedAt = Date.now();
      emit();
    }

    function addItem(chatId, entry) {
      const record = chat(chatId);
      if (!record) return null;
      const created = Object.assign({ id: uid(), createdAt: Date.now() }, entry);
      record.items.push(created);
      record.updatedAt = Date.now();
      emit();
      return created;
    }

    function updateItem(chatId, itemId, patch) {
      const existing = item(chatId, itemId);
      if (!existing) return;
      Object.assign(existing, patch);
      const record = chat(chatId);
      if (record) record.updatedAt = Date.now();
      emit();
    }

    function removeItem(chatId, itemId) {
      const record = chat(chatId);
      if (!record) return;
      const idx = record.items.findIndex(function (m) { return m.id === itemId; });
      if (idx >= 0) { record.items.splice(idx, 1); emit(); }
    }

    function appendToItem(chatId, itemId, text) {
      const existing = item(chatId, itemId);
      if (!existing) return;
      existing.text = (existing.text || '') + text;
      const record = chat(chatId);
      if (record) record.updatedAt = Date.now();
      emit();
    }

    function truncateAfter(chatId, itemId) {
      const record = chat(chatId);
      if (!record) return;
      const idx = record.items.findIndex(function (m) { return m.id === itemId; });
      if (idx >= 0) { record.items.length = idx + 1; emit(); }
    }

    function setQuery(q) { state.query = q; emit(); }
    function setTheme(t) { state.theme = t; emit(); }
    function setMode(mode) { state.mode = mode === 'verify' ? 'verify' : 'chat'; emit(); }
    function setSettings(patch) { Object.assign(state.settings, patch); emit(); }
    function setRunning(active_, controller) {
      state.running.active = !!active_;
      state.running.controller = controller || null;
      emit();
    }
    function setApiState(next, capabilities, ready, prompt) {
      state.apiState = next;
      if (capabilities !== undefined) state.capabilities = capabilities;
      if (ready !== undefined) state.ready = ready;
      if (prompt !== undefined) state.systemPrompt = prompt;
      emit();
    }

    function modelKeyValue() { return modelKey; }
    function setModelKey(value, remember) {
      modelKey = value || '';
      if (remember) storage.set('isnad.gui.modelkey.v1', modelKey);
      else storage.remove('isnad.gui.modelkey.v1');
    }

    const KEY = 'isnad.gui.state.v2';
    const SETTINGS_KEY = 'isnad.gui.settings.v2';
    const LEGACY_SETTINGS_KEY = 'isnad.gui.settings.v1';

    function load() {
      const saved = storage.get(KEY, null);
      if (saved && Array.isArray(saved.chats) && saved.chats.length) {
        state.chats = saved.chats;
        state.activeId = saved.activeId && saved.chats.some(function (c) { return c.id === saved.activeId; })
          ? saved.activeId : saved.chats[0].id;
      } else {
        const record = { id: uid(), title: 'New chat', items: [], createdAt: Date.now(), updatedAt: Date.now() };
        state.chats = [record];
        state.activeId = record.id;
      }

      /* Settings written by the previous build are read once, field by field,
         so an upgrade does not silently reset a working setup. Its chat log is
         deliberately not imported: those items were shaped for the previous
         renderer, and re-interpreting them risks showing a result card without
         the report that backs it. */
      const savedSettings = storage.get(SETTINGS_KEY, null) || storage.get(LEGACY_SETTINGS_KEY, null);
      if (savedSettings && typeof savedSettings === 'object') {
        try { state.settings.apiBase = Api.normalizeBase(savedSettings.apiBase); } catch (e) { state.settings.apiBase = ''; }
        try { state.settings.modelBase = Model.normalizeBase(savedSettings.modelBase); } catch (e) { state.settings.modelBase = ''; }
        if (typeof savedSettings.modelName === 'string') state.settings.modelName = savedSettings.modelName.slice(0, 120);
        if (typeof savedSettings.temperature === 'number') state.settings.temperature = Math.min(2, Math.max(0, savedSettings.temperature));
        if (savedSettings.direction === 'rtl' || savedSettings.direction === 'ltr') state.settings.direction = savedSettings.direction;
        if (typeof savedSettings.showTimestamps === 'boolean') state.settings.showTimestamps = savedSettings.showTimestamps;
        state.settings.modelRememberKey = savedSettings.modelRememberKey === true;
      }
      const remembered = storage.get('isnad.gui.modelkey.v1', '');
      if (state.settings.modelRememberKey && typeof remembered === 'string') modelKey = remembered;
      if (saved && (saved.mode === 'chat' || saved.mode === 'verify')) state.mode = saved.mode;
    }

    /* Written field by field: a new field has to be added here deliberately or
       it will not survive a reload. The api key is stored separately, and only
       when the user asked for it. */
    function serializableItem(m) {
      return {
        id: m.id,
        kind: m.kind,
        createdAt: m.createdAt,
        status: m.status || null,
        role: m.role || null,
        text: m.text || null,
        request: m.request || null,
        report: m.report || null,
        citations: m.citations || null,
        error: m.error || null,
        promptTokens: typeof m.tokens === 'number' ? m.tokens : null
      };
    }

    function persist() {
      storage.set(SETTINGS_KEY, state.settings);

      let chats = state.chats.slice(0, LIMITS.chats);
      for (let attempt = 0; attempt < 6; attempt += 1) {
        const ok = storage.set(KEY, {
          activeId: state.activeId,
          mode: state.mode,
          chats: chats.map(function (c) {
            return {
              id: c.id, title: c.title, createdAt: c.createdAt, updatedAt: c.updatedAt,
              items: c.items.slice(-LIMITS.items).map(serializableItem)
            };
          })
        });
        if (ok) return true;
        if (chats.length <= 1) { storage.remove(KEY); return false; }
        chats = chats.slice(0, Math.max(1, Math.ceil(chats.length / 2)));
      }
      return false;
    }

    return {
      state: state, subscribe: subscribe, emit: emit,
      chat: chat, active: active, item: item,
      createChat: createChat, selectChat: selectChat, deleteChat: deleteChat,
      renameChat: renameChat, addItem: addItem, updateItem: updateItem,
      removeItem: removeItem, appendToItem: appendToItem, truncateAfter: truncateAfter,
      setQuery: setQuery, setTheme: setTheme, setMode: setMode, setSettings: setSettings,
      setRunning: setRunning, setApiState: setApiState,
      modelKey: modelKeyValue, setModelKey: setModelKey,
      load: load, persist: persist
    };
  })();

  /* ==========================================================================
     8. RESULT AND ERROR CARDS
     ======================================================================= */

  function termValue(term, valueHtml, options) {
    const opts = options || {};
    return '<dt class="result__term">' + escapeHtml(term) + '</dt>' +
      '<dd class="result__value' + (opts.pending ? ' result__value--pending' : '') + '">' +
      valueHtml + '</dd>';
  }

  function quotedSpan(text) {
    return '<span dir="auto">' + escapeHtml(text) + '</span>';
  }

  function escapedLinkOrText(text, href) {
    const label = escapeHtml(text || 'Unnamed source');
    if (!href || !/^https?:\/\//.test(href)) return label;
    return '<a class="evidence__link" href="' + escapeHtml(href) + '" target="_blank" ' +
      'rel="noopener noreferrer nofollow">' + label + '</a>';
  }

  function sourceLine(meta) {
    if (!meta) return '<span class="result__value--pending">Not reported by the API</span>';
    return [
      escapedLinkOrText(meta.name, meta.url),
      meta.version ? 'v' + escapeHtml(meta.version) : '',
      meta.license ? escapeHtml(meta.license) : '',
      meta.content_sha256 ? 'sha256 ' + escapeHtml(shortHash(meta.content_sha256)) : ''
    ].filter(Boolean).join(' · ');
  }

  function renderEvidence(evidence) {
    const parts = [];
    parts.push('<div class="evidence__head">' +
      '<span class="evidence__ref" dir="auto">' + escapeHtml(evidence.reference || 'unknown reference') + '</span>' +
      '<span class="evidence__source">' +
        escapedLinkOrText(evidence.source_id, evidence.source_url) +
        (evidence.source_version ? ' · v' + escapeHtml(evidence.source_version) : '') +
      '</span>' +
    '</div>');

    parts.push('<p class="evidence__text" dir="auto">' + escapeHtml(evidence.source_text || '') + '</p>');

    if (evidence.matched_fragment) {
      parts.push('<p class="evidence__fragment" dir="auto">Matched fragment: ' +
        escapeHtml(evidence.matched_fragment) + '</p>');
    }

    const rows = [];
    const row = function (label, value, asLink) {
      if (!value) return;
      rows.push('<dt>' + escapeHtml(label) + '</dt><dd>' +
        (asLink
          ? escapedLinkOrText('Open source record', value)
          : '<span dir="auto">' + escapeHtml(value) + '</span>') +
      '</dd>');
    };
    row('Record title', evidence.record_title);
    row('Attribution', evidence.attribution_text);
    row('Bibliographic reference', evidence.bibliographic_reference);
    row('Grade', evidence.grade_text);
    row('Grade source', evidence.grade_source);
    row('Graded by', evidence.graded_by);
    row('Footnotes', evidence.footnotes);
    row('Source record', evidence.source_url, true);

    if (rows.length) parts.push('<dl class="evidence__meta">' + rows.join('') + '</dl>');

    if (!evidence.grade_text && !evidence.graded_by && !evidence.grade_source) {
      parts.push('<p class="grade-note">No grade or grader was supplied with this source record. ' +
        'This interface never infers one.</p>');
    }

    return '<div class="evidence">' + parts.join('') + '</div>';
  }

  function renderDifferences(diffs) {
    return '<div class="diff">' + diffs.map(function (d) {
      return '<div class="diff__row">' +
        '<span class="diff__kind">' + escapeHtml(humanizeToken(d.kind || 'difference')) + '</span>' +
        '<div class="diff__pair">' +
          '<span class="diff__text" data-side="submitted" dir="auto">Submitted: ' + escapeHtml(d.submitted_text || '—') + '</span>' +
          '<span class="diff__text" data-side="source" dir="auto">Source: ' + escapeHtml(d.source_text || '—') + '</span>' +
        '</div>' +
      '</div>';
    }).join('') + '</div>';
  }

  /* The report card: shared by model citations and by hand verification. */
  function renderReportCard(report, options) {
    const opts = options || {};
    const info = statusInfo(report.status);
    const meta = report.sourceMetadata;

    const flags = [];
    if (report.evidenceTruncated) {
      flags.push('<span class="connection-badge" data-state="checking">partial search coverage</span>');
    }
    if (typeof report.candidateCount === 'number' && report.candidateCount > 0) {
      flags.push('<span class="connection-badge">' + formatCount(report.candidateCount) + ' candidate(s) checked</span>');
    }

    const metaLine = [
      opts.heading || '',
      report.sourceType === 'hadith' ? 'Hadith' : (report.sourceType === 'quran' ? 'Qur’an' : report.sourceType),
      report.language === 'ar' ? 'Arabic' : (report.language === 'en' ? 'English' : report.language),
      meta && meta.version ? meta.name + ' v' + meta.version : '',
      formatDuration(opts.durationMs)
    ].filter(Boolean).join(' · ');

    const rows = [];
    rows.push(termValue('Quoted text',
      report.submittedQuote ? quotedSpan(report.submittedQuote) : '<span class="result__value--pending">No quote submitted</span>'));
    rows.push(termValue('Cited reference',
      report.citedReference ? quotedSpan(report.citedReference) : '<span class="result__value--pending">No reference submitted</span>'));
    rows.push(termValue('Matched references', report.matchedReferences && report.matchedReferences.length
      ? report.matchedReferences.map(quotedSpan).join(', ')
      : '<span class="result__value--pending">None reported for this status</span>'));
    rows.push(termValue('Source', sourceLine(meta)));
    if (meta && meta.coverage_note) rows.push(termValue('Coverage', quotedSpan(meta.coverage_note)));

    const body = [];
    body.push('<p class="result__explanation">' + escapeHtml(info.meaning) + '</p>');
    if (report.explanation) {
      body.push('<div class="result__note rich" dir="auto">' + MD.render(report.explanation) + '</div>');
    }
    if (flags.length) body.push('<div class="result__flags">' + flags.join('') + '</div>');
    body.push('<dl class="result__grid">' + rows.join('') + '</dl>');

    if (report.evidence && report.evidence.length) {
      body.push('<div>' + report.evidence.map(renderEvidence).join('') + '</div>');
    } else {
      body.push('<p class="result__note">No evidence records were returned with this result.</p>');
    }

    if (report.wordingDifferences && report.wordingDifferences.length) {
      body.push('<div><p class="result__note">Exact wording differences reported by the verifier:</p>' +
        renderDifferences(report.wordingDifferences) + '</div>');
    }

    return '<div class="result" data-tone="' + escapeHtml(info.tone) + '">' +
      '<div class="result__head">' +
        '<span class="result__status">' + escapeHtml(info.label) +
          ' <code dir="ltr">' + escapeHtml(report.status) + '</code></span>' +
        '<span class="result__badge">' + escapeHtml(metaLine) + '</span>' +
      '</div>' +
      '<div class="result__body">' + body.join('') + '</div>' +
    '</div>';
  }

  function renderErrorCard(error, badgeText) {
    const cancelled = error.tone === 'cancelled';
    return '<div class="result ' + (cancelled ? '' : 'result--error') + '" data-tone="' + escapeHtml(error.tone) + '">' +
      '<div class="result__head">' +
        '<span class="result__status">' + escapeHtml(error.title) + '</span>' +
        (badgeText ? '<span class="result__badge">' + escapeHtml(badgeText) + '</span>' : '') +
      '</div>' +
      '<div class="result__body">' +
        '<p class="result__explanation">' + escapeHtml(error.message) + '</p>' +
        (error.detail ? '<p class="result__note error-code">' + escapeHtml(error.detail) + '</p>' : '') +
        '<p class="result__note">Nothing about this citation was decided. A failed check is not a not-found result.</p>' +
      '</div>' +
    '</div>';
  }

  /* ==========================================================================
     9. CHAT ITEMS
     ======================================================================= */

  /* A citation card always has the same spine: the status, the source wording,
     then the verification detail. A pending card says only that a check is
     running — it never shows the model's version of the quote. */
  function citationCardHtml(citation) {
    if (citation.state === 'checking') {
      return '<div class="citation citation--checking" role="status">' +
        '<div class="citation__head">' +
          '<span class="citation__kind">Citation</span>' +
          '<span class="citation__ref" dir="auto">' +
            escapeHtml(citation.header.reference || 'reference not given') + '</span>' +
          '<span class="citation__state">checking source…</span>' +
        '</div>' +
        '<div class="citation__body">' +
          '<div class="checking">' +
            '<span class="typing__dot"></span><span class="typing__dot"></span><span class="typing__dot"></span>' +
            '<span class="checking__text">Holding the quotation until it has been checked</span>' +
          '</div>' +
        '</div>' +
      '</div>';
    }

    if (citation.state === 'error') {
      return '<div class="citation citation--error">' +
        '<div class="citation__head">' +
          '<span class="citation__kind">Citation</span>' +
          '<span class="citation__ref" dir="auto">' +
            escapeHtml(citation.header.reference || 'reference not given') + '</span>' +
          '<span class="citation__state">not checked</span>' +
        '</div>' +
        '<div class="citation__body">' + renderErrorCard(citation.error) + '</div>' +
      '</div>';
    }

    return '<div class="citation" data-tone="' + escapeHtml(statusInfo(citation.report.status).tone) + '">' +
      '<div class="citation__head">' +
        '<span class="citation__kind">Citation</span>' +
        '<span class="citation__ref" dir="auto">' +
          escapeHtml(citation.header.reference || citation.report.citedReference || 'reference not given') + '</span>' +
        '<span class="citation__state">' + escapeHtml(statusInfo(citation.report.status).label) + '</span>' +
      '</div>' +
      '<div class="citation__body">' + renderReportCard(citation.report, { durationMs: citation.durationMs }) + '</div>' +
    '</div>';
  }

  function renderUserItem(entry) {
    return '<p class="rich" dir="auto" style="margin:0;white-space:pre-wrap">' + escapeHtml(entry.text || '') + '</p>';
  }

  function renderVerifyResultItem(entry) {
    if (entry.kind === 'error') {
      return renderErrorCard(entry.error,
        (entry.request ? entry.request.source_type + ' · ' + formatDuration(entry.durationMs) : ''));
    }
    if (entry.kind === 'pending') {
      const request = entry.request || {};
      return '<div class="result" data-tone="info">' +
        '<div class="result__head">' +
          '<span class="result__status">Checking</span>' +
          '<span class="result__badge">' + escapeHtml(request.source_type + ' · ' + request.language) + '</span>' +
        '</div>' +
        '<div class="result__body">' +
          '<div class="checking" role="status">' +
            '<span class="typing__dot"></span><span class="typing__dot"></span><span class="typing__dot"></span>' +
            '<span class="checking__text">Waiting for the API at ' + escapeHtml(Api.describeBase()) + '…</span>' +
          '</div>' +
        '</div>' +
      '</div>';
    }
    if (entry.kind === 'request') {
      const request = entry.request || {};
      const facts = [
        request.source_type === 'hadith' ? 'Hadith' : 'Qur’an',
        request.language === 'ar' ? 'Arabic' : 'English',
        request.reference ? 'reference ' + request.reference : 'no reference'
      ].join(' · ');
      return '<p class="rich" dir="auto" style="margin:0;white-space:pre-wrap">' + escapeHtml(request.quote || '(reference only)') + '</p>' +
        '<p class="result__note" style="margin:var(--ui-space-2) 0 0">' + escapeHtml(facts) + '</p>';
    }
    return renderReportCard(entry.report, { durationMs: entry.durationMs });
  }

  function assistantContentHtml(entry) {
    const parts = [];
    if (entry.kind === 'assistant' || entry.kind === 'streaming') {
      /* The held citation cards are rendered inside the stream region, in
         order, so the reader sees prose → checking → verified card exactly
         where the model put the quotation. */
      parts.push('<div class="rich" data-rich></div>');
      (entry.citations || []).forEach(function (citation, index) {
        parts.push('<div class="citation-slot" data-citation-id="' + escapeHtml(String(citation.id || index)) + '"' +
          ' data-citation-index="' + index + '"></div>');
      });
    }
    if (entry.kind === 'error') {
      parts.push(renderErrorCard(entry.error, formatDuration(entry.durationMs)));
    }
    if (entry.kind === 'notice') {
      parts.push('<p class="msg__notice">' + escapeHtml(entry.text || '') + '</p>');
    }
    return parts.join('');
  }

  function itemElement(entry, chatId) {
    const article = document.createElement('article');
    const isUser = entry.role === 'user';
    article.className = 'msg msg--' + (isUser ? 'user' : 'assistant');
    if (entry.kind === 'pending' || entry.kind === 'streaming') article.classList.add('msg--streaming');
    article.dataset.itemId = entry.id;
    article.id = 'm-' + entry.id;

    const meta = ['<span class="msg__who">' + (isUser ? 'You' : 'ISNAD') + '</span>'];
    if (Store.state.settings.showTimestamps && entry.createdAt) {
      meta.push('<time class="msg__time" datetime="' + new Date(entry.createdAt).toISOString() + '">' +
        formatTime(entry.createdAt) + '</time>');
    }
    if (!isUser && entry.kind === 'assistant' && entry.model) {
      meta.push('<span class="msg__model">' + escapeHtml(entry.model) + '</span>');
    }

    let content = '';
    if (isUser) {
      content = renderUserItem(entry);
    } else if (entry.role === 'tool') {
      /* A manual check has its own pending and error panels. Only the model's
         placeholder item shows the typing dots. */
      content = renderVerifyResultItem(entry);
    } else if (entry.kind === 'pending') {
      content = '<div class="typing" role="status" aria-label="Waiting for the model">' +
        '<span class="typing__dot"></span><span class="typing__dot"></span><span class="typing__dot"></span>' +
      '</div>';
    } else {
      content = assistantContentHtml(entry);
    }

    const actions = [];
    if (entry.kind === 'error' && entry.request) {
      actions.push('<button class="iconbtn iconbtn--xs" type="button" data-act="retry" aria-label="Retry" title="Retry">' + icon('ic-refresh') + '</button>');
    }
    if (entry.kind === 'assistant' || entry.kind === 'streaming') {
      actions.push('<button class="iconbtn iconbtn--xs" type="button" data-act="copy" aria-label="Copy reply" title="Copy reply">' + icon('ic-copy') + '</button>');
      actions.push('<button class="iconbtn iconbtn--xs" type="button" data-act="copymd" aria-label="Copy as Markdown" title="Copy as Markdown">' + icon('ic-markdown') + '</button>');
      if (Speech.supported) {
        actions.push('<button class="iconbtn iconbtn--xs" type="button" data-act="speak" aria-label="Read aloud" title="Read aloud">' + icon('ic-speak') + '</button>');
      }
      actions.push('<button class="iconbtn iconbtn--xs" type="button" data-act="link" aria-label="Copy link to this reply" title="Copy link">' + icon('ic-anchor') + '</button>');
      actions.push('<span class="msg__sep" aria-hidden="true"></span>');
      actions.push('<button class="iconbtn iconbtn--xs" type="button" data-act="regenerate" aria-label="Regenerate reply" title="Regenerate">' + icon('ic-refresh') + '</button>');
      actions.push('<button class="iconbtn iconbtn--xs" type="button" data-act="up" aria-label="Good reply" title="Good reply"' + (entry.feedback === 'up' ? ' aria-pressed="true"' : '') + '>' + icon('ic-thumb-up') + '</button>');
      actions.push('<button class="iconbtn iconbtn--xs" type="button" data-act="down" aria-label="Poor reply" title="Poor reply"' + (entry.feedback === 'down' ? ' aria-pressed="true"' : '') + '>' + icon('ic-thumb-down') + '</button>');
      actions.push('<button class="iconbtn iconbtn--xs" type="button" data-act="prompt" aria-label="Inspect the citation protocol prompt" title="Citation protocol prompt">' + icon('ic-info') + '</button>');
    }
    if (isUser) {
      actions.push('<button class="iconbtn iconbtn--xs" type="button" data-act="edit" aria-label="Edit and resend" title="Edit and resend">' + icon('ic-pencil') + '</button>');
      actions.push('<button class="iconbtn iconbtn--xs" type="button" data-act="copy" aria-label="Copy message" title="Copy message">' + icon('ic-copy') + '</button>');
    }
    if (entry.kind !== 'pending') {
      actions.push('<span class="msg__sep" aria-hidden="true"></span>');
      actions.push('<button class="iconbtn iconbtn--xs" type="button" data-act="remove" aria-label="Remove message" title="Remove">' + icon('ic-trash') + '</button>');
    }

    article.innerHTML =
      '<span class="avatar avatar--md ' + (isUser ? 'avatar--user' : 'avatar--ai') + '" aria-hidden="true">' +
        '<svg><use href="#' + (isUser ? 'px-user' : 'px-spark') + '"/></svg>' +
      '</span>' +
      '<div class="msg__body">' +
        '<div class="msg__meta">' + meta.join('') + '</div>' +
        '<div class="msg__content">' + content + '</div>' +
        (entry.kind === 'assistant' && entry.stats
          ? '<p class="msg__stats">' + escapeHtml(entry.stats) + '</p>' : '') +
        (actions.length ? '<div class="msg__actions">' + actions.join('') + '</div>' : '') +
      '</div>';

    const rich = article.querySelector('[data-rich]');
    if (rich) Rich.paint(rich, entry.text || '');
    article.querySelectorAll('.citation-slot').forEach(function (slot) {
      const citation = (entry.citations || [])[Number(slot.dataset.citationIndex)];
      if (citation) slot.innerHTML = citationCardHtml(citation);
    });

    article.addEventListener('click', function (event) {
      const btn = event.target.closest('[data-act]');
      if (!btn) return;
      const act = btn.dataset.act;

      if (act === 'copy') {
        copyText(plainTextOf(entry));
      } else if (act === 'copymd') {
        copyText(markdownOf(entry), 'Markdown copied');
      } else if (act === 'speak') {
        if (Speech.isSpeaking(entry.id)) Speech.stop();
        else {
          Speech.speak(entry.id, plainTextOf(entry), btn, function () {
            btn.setAttribute('aria-pressed', 'false');
          });
          btn.setAttribute('aria-pressed', 'true');
        }
      } else if (act === 'link') {
        copyPermalink(entry.id);
      } else if (act === 'remove') {
        Store.removeItem(chatId, entry.id);
        Store.persist();
      } else if (act === 'regenerate') {
        regenerate(chatId, entry.id);
      } else if (act === 'up' || act === 'down') {
        const next = entry.feedback === act ? null : act;
        Store.updateItem(chatId, entry.id, { feedback: next });
        Store.persist();
        const row = btn.parentElement;
        $$('[data-act="up"], [data-act="down"]', row).forEach(function (b) {
          if (b.dataset.act === next) b.setAttribute('aria-pressed', 'true');
          else b.removeAttribute('aria-pressed');
        });
      } else if (act === 'edit') {
        editMessage(chatId, entry.id);
      } else if (act === 'retry') {
        if (entry.request) runVerifyFlow(chatId, entry.request, { afterItemId: entry.id });
      } else if (act === 'prompt') {
        openPromptDialog();
      }
    });

    return article;
  }

  /* Spoken and copied text carries the verified source wording and the match
     status together, because that is what the reader was shown: the model's
     unverified version of a quotation is never repeated as if it were a quote. */
  function plainTextOf(entry) {
    const parts = [];
    if (entry.text) parts.push(entry.text);
    (entry.citations || []).forEach(function (citation) {
      if (citation.state !== 'done' || !citation.report) {
        if (citation.state === 'error' && citation.error) {
          parts.push('Citation not checked: ' + citation.error.title + '.');
        }
        return;
      }
      const reference = citation.report.matchedReferences[0] || citation.report.citedReference || '';
      const text = citation.report.evidence.length ? citation.report.evidence[0].source_text : '';
      const line = [text, statusInfo(citation.report.status).label].filter(Boolean).join(' — ');
      parts.push('[' + (reference || 'citation') + '] ' + line);
    });
    return parts.join('\n\n').trim();
  }

  function markdownOf(entry) {
    const parts = [];
    if (entry.text) parts.push(entry.text);
    (entry.citations || []).forEach(function (citation) {
      if (citation.state !== 'done' || !citation.report) return;
      const report = citation.report;
      parts.push('> ' + (report.matchedReferences[0] || report.citedReference || 'reference'));
      if (report.evidence.length) parts.push('> ' + report.evidence[0].source_text);
      parts.push('>');
      parts.push('> Status: `' + report.status + '` — ' + statusInfo(report.status).meaning);
      if (report.evidence.length && report.evidence[0].grade_text) {
        parts.push('> Grade as supplied by the source: ' + report.evidence[0].grade_text +
          (report.evidence[0].grade_source ? ' (' + report.evidence[0].grade_source + ')' : ''));
      }
    });
    return parts.join('\n\n').trim();
  }

  const Speech = (function () {
    const supported = typeof window !== 'undefined' &&
      'speechSynthesis' in window &&
      typeof window.SpeechSynthesisUtterance === 'function';
    let current = null;

    function stop() {
      if (!supported) return;
      const previous = current;
      current = null;
      if (previous && previous.button) previous.button.setAttribute('aria-pressed', 'false');
      try { window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
    }

    function speak(msgId, text, button, onEnd) {
      if (!supported) return;
      stop();
      const plain = String(text || '')
        .replace(/```[\s\S]*?```/g, ' code block ')
        .replace(/`([^`]+)`/g, '$1')
        .replace(/^[#>\-*\s]+/gm, '')
        .replace(/\*\*([^*]+)\*\*/g, '$1')
        .replace(/\s+/g, ' ')
        .trim();
      if (!plain) return;
      const utterance = new window.SpeechSynthesisUtterance(plain);
      utterance.onend = function () { if (current && current.msgId === msgId) stop(); else if (onEnd) onEnd(); };
      utterance.onerror = function () { if (current && current.msgId === msgId) stop(); else if (onEnd) onEnd(); };
      current = { msgId: msgId, button: button || null };
      try { window.speechSynthesis.speak(utterance); } catch (e) { stop(); }
    }

    function isSpeaking(msgId) {
      return !!current && (msgId === undefined || current.msgId === msgId);
    }

    return { supported: supported, speak: speak, stop: stop, isSpeaking: isSpeaking };
  })();

  function copyText(text, message) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { Toasts.show(message || 'Copied', 'success'); },
        function () { Toasts.show('Could not copy', 'error'); }
      );
    } else {
      Toasts.show('Clipboard unavailable', 'error');
    }
  }

  function copyPermalink(itemId) {
    try { window.history.replaceState(null, '', '#m-' + itemId); } catch (e) { /* file:// */ }
    copyText(window.location.href, 'Link copied');
    highlightItem(itemId);
  }

  function highlightItem(itemId) {
    const node = document.getElementById('m-' + itemId);
    if (!node) return;
    node.scrollIntoView({ block: 'center', behavior: 'smooth' });
    node.classList.remove('msg--target');
    void node.offsetWidth;
    node.classList.add('msg--target');
    window.setTimeout(function () { node.classList.remove('msg--target'); }, 2400);
  }

  /* ==========================================================================
     10. CHAT TRANSCRIPT
     ======================================================================= */

  function renderItems(options) {
    const opts = options || {};
    const container = $('#messages');
    const record = Store.active();
    container.innerHTML = '';

    if (Store.state.mode === 'verify') {
      renderVerifySurface(container, record);
      if (opts.scroll !== false) scrollToBottom(opts.instant === true);
      return;
    }

    if (!record || !record.items.length) {
      container.appendChild(emptyState());
      if (opts.scroll !== false) scrollToBottom(opts.instant === true);
      return;
    }

    record.items.forEach(function (entry) { container.appendChild(itemElement(entry, record.id)); });

    if (Store.state.running.active && Store.state.running.kind === 'chat') {
      const wrap = document.createElement('div');
      wrap.className = 'stoprow';
      const stopBtn = document.createElement('button');
      stopBtn.type = 'button';
      stopBtn.className = 'btn btn--ghost btn--sm';
      stopBtn.innerHTML = icon('ic-stop') + 'Stop generating';
      stopBtn.addEventListener('click', stopGeneration);
      wrap.appendChild(stopBtn);
      container.appendChild(wrap);
    }

    if (opts.scroll !== false) scrollToBottom(opts.instant === true);
  }

  /* Painting the streaming message only. The transcript container is left
     alone: rebuilding it on every token would drop the reader's scroll
     position and rerun every card in the conversation. */
  function patchStreamingItem(chatId, itemId) {
    const entry = Store.item(chatId, itemId);
    const article = document.querySelector('[data-item-id="' + itemId + '"]');
    if (!entry || !article) return false;

    const rich = article.querySelector('[data-rich]');
    if (rich) Rich.paint(rich, entry.text || '');

    article.querySelectorAll('.citation-slot').forEach(function (slot) {
      const citation = (entry.citations || [])[Number(slot.dataset.citationIndex)];
      if (!citation) return;
      const html = citationCardHtml(citation);
      if (slot.dataset.rendered === citation.state && slot.dataset.renderedRef === String(citation.header.reference)) return;
      slot.innerHTML = html;
      slot.dataset.rendered = citation.state;
      slot.dataset.renderedRef = String(citation.header.reference);
    });
    return true;
  }

  /* ---- Empty states --------------------------------------------------- */

  function chatEmptyState() {
    const wrap = document.createElement('div');
    wrap.className = 'empty';
    const configured = Model.configured();
    const ready = Store.state.apiState === 'ready';

    const prompts = Store.state.chatPrompts || [];

    wrap.innerHTML =
      '<svg class="empty__mark" aria-hidden="true"><use href="#px-mark"/></svg>' +
      '<div>' +
        '<h2 class="empty__title">' + (configured ? 'Ask anything' : 'Connect a model to chat') + '</h2>' +
        '<p class="empty__sub">' + (configured
          ? 'Answers stream normally. Any verse or hadith the model quotes is held, checked against the pinned source, and shown as a verified card.'
          : 'Add a model endpoint and key in Model settings, and this interface will inject the citation protocol automatically.') + '</p>' +
      '</div>' +
      (ready ? '' : '<div class="empty__caps"><p class="empty__note">' +
        (hasNoApiOrigin()
          ? 'This file was opened from disk. Open Connection settings and enter the verification API base URL — citations cannot be checked without it.'
          : 'The verification API is unreachable, so quotations cannot be checked yet. Chat still needs it; only prose would be trustworthy without it.') +
        '</p></div>') +
      '<div class="suggestions" role="list"></div>';

    const grid = $('.suggestions', wrap);
    (prompts.length ? prompts : [
      { label: 'Verse', text: 'Quote Sūrat al-Ikhlāṣ with its reference.' },
      { label: 'Hadith', text: 'Quote a hadith about intention, with the reference you have.' },
      { label: 'Ask', text: 'What does the Qur’an say about patience?' }
    ]).forEach(function (prompt) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'suggestion';
      btn.setAttribute('role', 'listitem');
      btn.innerHTML = '<span class="suggestion__label">' + escapeHtml(prompt.label) + '</span>' +
        '<span class="suggestion__text">' + escapeHtml(prompt.text) + '</span>';
      btn.addEventListener('click', function () {
        if (!configured) { openSettings(); return; }
        const input = $('#composerInput');
        input.value = prompt.text;
        autoGrow(input);
        updateSendState();
        sendMessage();
      });
      grid.appendChild(btn);
    });

    return wrap;
  }

  const STATUS_ORDER = [
    'exact_match', 'normalized_match', 'partial_match', 'mismatch_at_cited_reference',
    'quote_found_wrong_reference', 'reference_found_without_quote',
    'not_found_in_checked_corpus', 'ambiguous_multiple_matches', 'unsupported_source_or_language'
  ];

  function verifyEmptyState() {
    const wrap = document.createElement('div');
    wrap.className = 'empty';
    const caps = Store.state.capabilities;

    let capsHtml;
    if (Store.state.apiState === 'checking') {
      capsHtml = '<p class="empty__note">Reading <code>/v1/capabilities</code>…</p>';
    } else if (!caps) {
      capsHtml = '<p class="empty__note">' + (hasNoApiOrigin()
        ? 'This file was opened from disk, so no API is configured yet. Open Connection settings and enter the API base URL.'
        : 'The API could not be reached, so supported sources and limits are unknown. Set the base URL in Connection settings and refresh.') + '</p>';
    } else {
      const rows = [];
      rows.push(termValue('API version', escapeHtml(String(caps.api_version || 'unreported'))));
      rows.push(termValue('Statuses', escapeHtml(String((caps.statuses || []).length)) + ' textual-correspondence outcomes'));
      rows.push(termValue('Quote limit', escapeHtml(formatCount(quoteLimit())) + ' characters'));
      if (caps.limits && caps.limits.max_websocket_message_bytes) {
        rows.push(termValue('Streaming', 'WebSocket ' + escapeHtml(caps.streaming && caps.streaming.path || '/v1/stream') +
          ' · ' + escapeHtml(formatCount(Math.round(caps.limits.max_websocket_message_bytes / 1024))) + ' KiB messages'));
      }
      capsHtml = '<dl class="result__grid">' + rows.join('') + '</dl>' +
        '<p class="result__note" style="margin-top:var(--ui-space-3)">' +
        escapeHtml(humanizeToken(caps.status_semantics)) + '.</p>';
    }

    const statusRows = STATUS_ORDER.map(function (status) {
      const info = statusInfo(status);
      return termValue(info.label, escapeHtml(info.meaning) + '<br><code class="status-code" dir="ltr">' + escapeHtml(status) + '</code>');
    }).join('');

    wrap.innerHTML =
      '<svg class="empty__mark" aria-hidden="true"><use href="#px-mark"/></svg>' +
      '<div>' +
        '<h2 class="empty__title">Check a quotation by hand</h2>' +
        '<p class="empty__sub">Paste a quoted verse or hadith and the reference you were given. ' +
        'The answer comes from the pinned source data, with the source wording shown verbatim.</p>' +
      '</div>' +
      '<div class="empty__caps">' + capsHtml + '</div>' +
      '<details class="empty__caps guide">' +
        '<summary class="suggestion__label">What each status means</summary>' +
        '<dl class="result__grid" style="margin-top:var(--ui-space-3)">' + statusRows + '</dl>' +
        '<p class="result__note">None of these is a hadith grade, an authenticity ruling, or a religious judgement.</p>' +
      '</details>';

    if (caps && caps.sources && caps.sources.length) {
      const list = document.createElement('div');
      list.className = 'empty__caps';
      list.innerHTML = '<div class="guide">' +
        '<h3 class="set-section__title">Supported in this deployment</h3>' +
        '<dl class="result__grid">' + caps.sources.map(function (s) {
          return termValue(s.source_type + ' / ' + s.language,
            escapeHtml(s.name) + ' · v' + escapeHtml(s.source_version) + ' · ' + escapeHtml(s.mode) +
            '<br><span class="result__note">' + escapeHtml(s.reference_format || '') +
            (s.coverage_note ? ' — ' + escapeHtml(s.coverage_note) : '') + '</span>');
        }).join('') + '</dl>' +
        (Store.state.ready && Store.state.ready.sources
          ? '<p class="result__note">Readiness reports ' + escapeHtml(String(Store.state.ready.sources.length)) +
            ' loaded source edition(s). A remote source being ready does not mean it is reachable right now.</p>'
          : '') +
      '</div>';
      wrap.appendChild(list);
    }

    return wrap;
  }

  function emptyState() {
    return Store.state.mode === 'verify' ? verifyEmptyState() : chatEmptyState();
  }

  /* The verify surface reuses the chat transcript: hand checks are items in the
     same conversation, so nothing needs a second history. */
  function renderVerifySurface(container, record) {
    const items = record ? record.items.filter(function (entry) {
      return entry.role === 'tool' || entry.kind === 'pending' || entry.kind === 'request' || entry.kind === 'error';
    }) : [];
    if (!items.length) {
      container.appendChild(verifyEmptyState());
      return;
    }
    items.forEach(function (entry) { container.appendChild(itemElement(entry, record.id)); });
  }

  /* ==========================================================================
     11. MESSAGE FLOW — model streaming with citation gating
     ======================================================================= */

  function quoteLimit() {
    const caps = Store.state.capabilities;
    return caps && caps.limits && caps.limits.max_quote_characters ? caps.limits.max_quote_characters : 4000;
  }

  function chatMessagesFor(record, upToIndex) {
    const messages = [];
    const prompt = Store.state.systemPrompt && Store.state.systemPrompt.prompt;
    if (prompt) messages.push({ role: 'system', content: prompt });

    const items = record.items.slice(0, upToIndex === undefined ? record.items.length : upToIndex);
    items.forEach(function (entry) {
      if (entry.role === 'user' && entry.kind !== 'pending') {
        messages.push({ role: 'user', content: entry.text || '' });
      } else if (entry.kind === 'assistant' && entry.text) {
        /* The assistant's own text is replayed exactly as written, markers
           included: the protocol is part of the conversation the model sees. */
        messages.push({ role: 'assistant', content: entry.text + citationSuffix(entry) });
      }
    });
    return messages;
  }

  function citationSuffix(entry) {
    return (entry.citations || []).map(function (citation) {
      return '\n\n[[ISNAD-CITATION source=' + citation.header.source +
        ' language=' + citation.header.language +
        (citation.header.reference ? ' reference=' + citation.header.reference : '') +
        ']]' + (citation.modelQuote || '') + '[[/ISNAD-CITATION]]';
    }).join('');
  }

  function autoGrow(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }

  function updateSendState() {
    const input = $('#composerInput');
    const btn = $('#sendBtn');
    const text = input.value.trim();
    const busy = Store.state.running.active;
    const verifyMode = Store.state.mode === 'verify';

    if (verifyMode) {
      const request = readVerifyForm();
      const problem = validateVerifyRequest(request);
      btn.disabled = Boolean(problem) || busy;
      btn.title = problem ? problem.message : 'Verify against the pinned source';
    } else {
      const missing = !Model.configured();
      btn.disabled = busy || missing || !text;
      btn.title = missing ? 'Configure a model first' : 'Send';
    }
    btn.setAttribute('aria-disabled', String(btn.disabled));
  }

  function currentAbort() {
    return Store.state.running.controller;
  }

  function stopGeneration() {
    const controller = currentAbort();
    if (controller) controller.abort();
  }

  async function sendMessage() {
    if (Store.state.running.active) return;
    if (!Model.configured()) { openSettings(); return; }

    const input = $('#composerInput');
    const text = input.value.trim();
    if (!text) return;

    let record = Store.active();
    if (!record) record = Store.createChat();

    Store.addItem(record.id, { role: 'user', kind: 'message', text: text, status: 'complete' });

    if (record.items.filter(function (m) { return m.role === 'user'; }).length === 1) {
      Store.renameChat(record.id, text.length > 46 ? text.slice(0, 46).trim() + '…' : text);
    }

    input.value = '';
    autoGrow(input);
    updateSendState();
    Store.persist();

    await runAssistant(record.id);
  }

  async function runAssistant(chatId) {
    const record = Store.chat(chatId);
    if (!record) return;

    const controller = new AbortController();
    Store.setRunning(true, controller);
    Store.state.running.kind = 'chat';
    updateSendState();

    const placeholder = Store.addItem(chatId, {
      role: 'assistant', kind: 'pending', status: 'pending'
    });
    scrollToBottom();

    const streamer = window.IsnadCitationStream.createStreamer();
    const startedAt = Date.now();
    let text = '';
    let citations = [];

    /* One block at a time: a new citation can only start after the previous
       one has been verified, so the cards keep their order. */
    let queue = Promise.resolve();

    function applyEvents(events) {
      events.forEach(function (event) {
        if (event.type === 'prose') {
          text += event.text;
          Store.updateItem(chatId, placeholder.id, { kind: 'streaming', status: 'streaming', text: text, citations: citations });
        } else if (event.type === 'citation_start') {
          citations = citations.concat([{
            id: event.id,
            header: event.header,
            state: 'checking',
            report: null,
            error: null
          }]);
          Store.updateItem(chatId, placeholder.id, { kind: 'streaming', status: 'streaming', text: text, citations: citations });
        } else if (event.type === 'citation_end') {
          const pendingCitation = citations[citations.length - 1];
          if (!pendingCitation || pendingCitation.id !== event.id) return;
          pendingCitation.modelQuote = event.quote;
          pendingCitation.state = 'checking';
          Store.updateItem(chatId, placeholder.id, { kind: 'streaming', status: 'streaming', text: text, citations: citations });
          const snapshot = citations;
          const targetId = placeholder.id;
          queue = queue.then(function () {
            return verifyCitation(chatId, targetId, snapshot, pendingCitation, controller, startedAt);
          });
        } else if (event.type === 'citation_incomplete') {
          const note = 'The model opened a quotation and stopped before closing it. ' +
            'The held text (' + event.withheld_characters + ' characters) was not shown or checked.';
          citations = citations.concat([{
            id: event.id,
            header: { source: 'unknown', language: 'unknown', reference: '' },
            state: 'error',
            error: { title: 'Incomplete citation', tone: 'error', message: note, detail: '' }
          }]);
          Store.updateItem(chatId, placeholder.id, { kind: 'streaming', status: 'streaming', text: text, citations: citations });
        } else if (event.type === 'citation_invalid') {
          const reasons = {
            malformed_header: 'The model wrote a citation marker whose header did not parse (a required field was missing or repeated).',
            oversized_header: 'The model wrote a citation marker with no end.',
            unterminated_header: 'The model opened a citation marker and never finished it.'
          };
          citations = citations.concat([{
            id: event.id || 0,
            header: { source: 'unknown', language: 'unknown', reference: '' },
            state: 'error',
            error: {
              title: 'Rejected citation marker', tone: 'error',
              message: (reasons[event.reason] || 'A citation marker was rejected.') +
                ' The marked text was withheld and not checked.',
              detail: 'reason: ' + event.reason
            }
          }]);
          Store.updateItem(chatId, placeholder.id, { kind: 'streaming', status: 'streaming', text: text, citations: citations });
        }
      });
      patchStreamingItem(chatId, placeholder.id);
    }

    try {
      Store.updateItem(chatId, placeholder.id, {
        kind: 'streaming', status: 'streaming', text: '', citations: [], model: Model.label()
      });

      const messages = chatMessagesFor(Store.chat(chatId), Store.chat(chatId).items.length - 1);
      await Model.streamChat(messages, {
        signal: controller.signal,
        onText: function (chunk) { applyEvents(streamer.push(chunk)); }
      });

      applyEvents(streamer.finish());
      await queue;

      const elapsed = Date.now() - startedAt;
      const checked = citations.filter(function (c) { return c.state === 'done'; }).length;
      const failed = citations.filter(function (c) { return c.state === 'error'; }).length;
      const stats = [
        formatDuration(elapsed),
        checked ? checked + (checked === 1 ? ' citation checked' : ' citations checked') : 'no citations',
        failed ? failed + ' not checked' : ''
      ].filter(Boolean).join(' · ');

      Store.updateItem(chatId, placeholder.id, {
        kind: 'assistant', status: 'complete', text: text, citations: citations, stats: stats
      });
    } catch (err) {
      applyEvents(streamer.finish());
      const described = describeModelError(err);
      if (described.tone === 'cancelled') {
        Store.updateItem(chatId, placeholder.id, {
          kind: 'assistant', status: 'complete', text: text, citations: citations,
          stats: formatDuration(Date.now() - startedAt) + ' · stopped'
        });
      } else {
        Store.updateItem(chatId, placeholder.id, {
          kind: 'error', status: 'error', text: text, citations: citations,
          error: described, durationMs: Date.now() - startedAt
        });
        Toasts.show(described.title, 'error');
      }
    } finally {
      Store.setRunning(false);
      Store.state.running.kind = null;
      updateSendState();
      Store.persist();
      scrollToBottom();
    }
  }

  /* Verify one held quotation and fold the answer into the card. */
  async function verifyCitation(chatId, itemId, citations, citation, controller, startedAt) {
    const request = {
      source_type: citation.header.source,
      language: citation.header.language,
      quote: citation.modelQuote || null,
      reference: citation.header.reference || null
    };
    try {
      const result = await Api.verify(request, { signal: controller.signal });
      citation.state = 'done';
      citation.durationMs = result.durationMs;
      citation.report = normalizeReport(result.payload);
    } catch (err) {
      const described = describeError(err);
      citation.state = 'error';
      citation.error = described;
    }
    citation.modelQuote = citation.modelQuote || '';
    const current = Store.item(chatId, itemId);
    if (!current) return;
    Store.updateItem(chatId, itemId, {
      kind: 'streaming', status: 'streaming', text: current.text, citations: citations
    });
    patchStreamingItem(chatId, itemId);
  }

  function normalizeReport(payload) {
    return {
      status: payload.status,
      sourceType: payload.source_type,
      language: payload.language,
      sourceMetadata: payload.source_metadata || null,
      submittedQuote: payload.submitted_quote || null,
      citedReference: payload.cited_reference || null,
      matchedReferences: payload.matched_references || [],
      evidence: (payload.evidence || []).map(function (e) {
        return {
          reference: e.reference, source_text: e.source_text, matched_fragment: e.matched_fragment,
          source_id: e.source_id, source_version: e.source_version, source_url: e.source_url,
          footnotes: e.footnotes, record_title: e.record_title, attribution_text: e.attribution_text,
          grade_text: e.grade_text, grade_source: e.grade_source, graded_by: e.graded_by,
          bibliographic_reference: e.bibliographic_reference
        };
      }),
      wordingDifferences: (payload.wording_differences || []).map(function (d) {
        return { kind: d.kind, submitted_text: d.submitted_text, source_text: d.source_text };
      }),
      explanation: payload.explanation || '',
      candidateCount: typeof payload.candidate_count === 'number' ? payload.candidate_count : null,
      evidenceTruncated: payload.evidence_truncated === true
    };
  }

  function regenerate(chatId, itemId) {
    const record = Store.chat(chatId);
    if (!record) return;
    const idx = record.items.findIndex(function (m) { return m.id === itemId; });
    if (idx < 1) return;
    record.items.length = idx;
    Store.emit();
    Store.persist();
    runAssistant(chatId);
  }

  function editMessage(chatId, itemId) {
    const entry = Store.item(chatId, itemId);
    if (!entry) return;
    const input = $('#composerInput');
    input.value = entry.text || '';
    autoGrow(input);
    updateSendState();
    input.focus();
    Store.truncateAfter(chatId, itemId);
    Store.persist();
  }

  /* ==========================================================================
     12. VERIFY-SURFACE FLOW (manual)
     ======================================================================= */
  function readVerifyForm() {
    return {
      source_type: $('#sourceSelect').value,
      language: $('#languageSelect').value,
      quote: $('#composerInput').value,
      reference: $('#referenceInput').value.trim()
    };
  }

  function validateVerifyRequest(request) {
    const quote = (request.quote || '').trim();
    const reference = (request.reference || '').trim();
    if (!quote && !reference) return { message: 'Provide quoted text, a source reference, or both.' };
    if (request.quote.length > quoteLimit()) {
      return { message: 'The quote is ' + formatCount(request.quote.length) + ' characters; the API limit is ' + formatCount(quoteLimit()) + '.' };
    }
    if (reference.length > 64) return { message: 'A reference may be at most 64 characters.' };
    const caps = Store.state.capabilities;
    if (caps && Array.isArray(caps.sources)) {
      const supported = caps.sources.some(function (s) {
        return s.source_type === request.source_type && s.language === request.language;
      });
      if (!supported) {
        return { message: 'No adapter is configured for ' + request.source_type + ' / ' + request.language + ' in this deployment.' };
      }
    }
    return null;
  }

  function supportedPair() {
    const caps = Store.state.capabilities;
    if (!caps || !Array.isArray(caps.sources)) return null;
    return caps.sources.find(function (s) {
      return s.source_type === $('#sourceSelect').value && s.language === $('#languageSelect').value;
    }) || null;
  }

  async function runVerifyFlow(chatId, request, options) {
    const opts = options || {};
    if (Store.state.running.active) { Toasts.show('A request is already running', 'error'); return; }
    if (opts.afterItemId) Store.truncateAfter(chatId, opts.afterItemId);

    const pending = Store.addItem(chatId, { role: 'tool', kind: 'pending', status: 'pending', request: request });
    const controller = new AbortController();
    Store.setRunning(true, controller);
    Store.state.running.kind = 'verify';
    scrollToBottom();

    try {
      const result = await Api.verify(request, { signal: controller.signal });
      Store.updateItem(chatId, pending.id, {
        kind: 'report', status: 'complete', durationMs: result.durationMs,
        report: normalizeReport(result.payload)
      });
      Store.persist();
      refreshApiState();
    } catch (err) {
      const described = describeError(err);
      Store.updateItem(chatId, pending.id, {
        kind: 'error', status: described.tone === 'cancelled' ? 'cancelled' : 'error',
        durationMs: Date.now() - pending.createdAt, error: described, request: request
      });
      Store.persist();
      if (described.tone !== 'cancelled') Toasts.show(described.title, 'error');
    } finally {
      Store.setRunning(false);
      Store.state.running.kind = null;
      updateSendState();
      scrollToBottom();
    }
  }

  function sendVerifyRequest() {
    const request = readVerifyForm();
    const problem = validateVerifyRequest(request);
    if (problem) { Toasts.show(problem.message, 'error'); $('#composerInput').focus(); return; }

    let record = Store.active();
    if (!record) record = Store.createChat();

    const trimmed = {
      source_type: request.source_type, language: request.language,
      quote: request.quote.trim() || null, reference: request.reference || null
    };
    Store.addItem(record.id, { role: 'tool', kind: 'request', status: 'complete', request: trimmed });
    if (record.items.filter(function (m) { return m.role === 'tool'; }).length === 1) {
      Store.renameChat(record.id, 'Check · ' + (trimmed.reference || (trimmed.quote || '').slice(0, 32)));
    }
    $('#composerInput').value = '';
    autoGrow($('#composerInput'));
    updateSendState();
    Store.persist();
    runVerifyFlow(record.id, trimmed);
  }

  /* ==========================================================================
     13. API STATE
     ======================================================================= */
  async function refreshApiState() {
    Store.setApiState('checking');
    try {
      const caps = await Api.capabilities({ timeoutMs: 8000 });
      let ready = null;
      let prompt = Store.state.systemPrompt;
      try {
        const probe = await Api.ready({ timeoutMs: 8000 });
        ready = probe.payload;
      } catch (e) { /* readiness is informational */ }
      if (!prompt || prompt.version !== (caps.payload.system_prompt_version || prompt.version)) {
        try {
          const fetched = await Api.systemPrompt({ timeoutMs: 8000 });
          prompt = fetched.payload;
        } catch (e) { /* chat stays disabled without the protocol prompt */ }
      }
      Store.setApiState('ready', caps.payload, ready, prompt);
    } catch (err) {
      Store.setApiState('unavailable', null, null, Store.state.systemPrompt);
      if (err && err.isnad && err.isnad.code !== 'cancelled') {
        const described = describeError(err);
        Toasts.show(Api.base() ? described.title + ' — ' + Api.base() : described.title, 'error');
      }
    }
  }

  function renderConnection() {
    const badge = $('#apiBadge');
    const state = Store.state;
    const labels = { checking: 'API CHECKING', ready: 'API READY', unavailable: 'API UNREACHABLE' };
    badge.setAttribute('data-state', state.apiState);
    badge.textContent = labels[state.apiState] || 'API';

    const caps = state.capabilities;
    const ready = state.ready;
    const parts = [Api.base() || hasNoApiOrigin() ? '' : 'Connected to ' + Api.describeBase()];
    if (!Api.base() && hasNoApiOrigin()) parts.push('No API configured — open Connection settings');
    if (caps) parts.push('API version ' + (caps.api_version || 'unreported'));
    if (caps && caps.sources) parts.push(caps.sources.length + ' source/language pairs');
    if (ready && ready.sources) parts.push(ready.sources.length + ' loaded editions');
    if (state.apiState === 'ready' && !state.systemPrompt) parts.push('Citation protocol prompt unavailable — chat is disabled');
    if (state.apiState === 'unavailable') {
      parts.push(hasNoApiOrigin()
        ? 'Enter the API base URL in Connection settings'
        : 'Unreachable — check the base URL and CORS allow-list');
    }
    if (state.apiState === 'ready' && !ready) parts.push('Readiness probe did not answer');
    badge.title = parts.filter(Boolean).join(' · ');

    const modelBadge = $('#modelBadge');
    const configured = Model.configured();
    modelBadge.setAttribute('data-state', configured ? 'ready' : 'unavailable');
    modelBadge.textContent = configured ? Model.label() : 'NO MODEL';
    modelBadge.title = configured
      ? 'Model endpoint ' + Store.state.settings.modelBase +
        (Store.modelKey() ? ' · key held ' + (Store.state.settings.modelRememberKey ? 'on this device' : 'in memory only') : ' · no key set')
      : 'Open Model settings to connect a model';
  }

  /* ==========================================================================
     14. CHAT LIST, SIDEBAR, THEME, EXPORT
     ======================================================================= */
  function groupLabel(ts) {
    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    if (ts >= startOfToday) return 'Today';
    if (ts >= startOfToday - 86400000) return 'Yesterday';
    if (ts >= startOfToday - 86400000 * 7) return 'Previous 7 days';
    if (new Date(ts).getFullYear() === now.getFullYear()) return 'Earlier this year';
    return 'Older';
  }

  function renderChatList() {
    const list = $('#convList');
    const state = Store.state;
    const query = state.query.trim().toLowerCase();

    const matches = state.chats.filter(function (c) {
      if (!query) return true;
      if (c.title.toLowerCase().indexOf(query) >= 0) return true;
      return c.items.some(function (m) {
        if ((m.text || '').toLowerCase().indexOf(query) >= 0) return true;
        if (m.request && (m.request.quote || '').toLowerCase().indexOf(query) >= 0) return true;
        if (m.report && (m.report.submittedQuote || '').toLowerCase().indexOf(query) >= 0) return true;
        return false;
      });
    });

    const count = $('#historyCount');
    count.hidden = !query;
    if (query) {
      count.textContent = matches.length + ' of ' + state.chats.length +
        (matches.length === 1 ? ' conversation matches' : ' conversations match');
    }

    list.innerHTML = '';
    if (!matches.length) {
      const empty = document.createElement('div');
      empty.className = 'sidebar__group';
      empty.textContent = query ? 'No matching conversations' : 'No conversations yet';
      list.appendChild(empty);
      return;
    }

    const sorted = matches.slice().sort(function (a, b) { return b.updatedAt - a.updatedAt; });
    let group = '';
    sorted.forEach(function (record) {
      const label = groupLabel(record.updatedAt);
      if (label !== group) {
        group = label;
        const heading = document.createElement('div');
        heading.className = 'sidebar__group';
        heading.textContent = label;
        list.appendChild(heading);
      }

      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'conv';
      btn.setAttribute('aria-current', String(record.id === state.activeId));
      btn.dataset.chatId = record.id;
      btn.innerHTML = icon('ic-chat', 'conv__icon') +
        '<span class="conv__title">' + escapeHtml(record.title) + '</span>';
      btn.addEventListener('click', function () {
        Store.selectChat(record.id);
        scrollToBottom(true);
        if (isNarrow()) setSidebar('collapsed');
      });

      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'conv__del';
      del.setAttribute('aria-label', 'Delete “' + record.title + '”');
      del.title = 'Delete';
      del.innerHTML = icon('ic-trash');
      del.addEventListener('click', function (event) {
        event.stopPropagation();
        confirmDelete(record.id);
      });

      btn.appendChild(del);
      list.appendChild(btn);
    });
  }

  function confirmDelete(id) {
    const record = Store.chat(id);
    if (!record) return;
    if (!window.confirm('Delete “' + record.title + '” and its messages? This cannot be undone.')) return;
    Store.deleteChat(id);
    Store.persist();
    Toasts.show('Conversation deleted', 'success');
  }

  function isNarrow() { return window.matchMedia('(max-width: 900px)').matches; }

  function setSidebar(mode) {
    const shell = $('#shell');
    const sidebar = $('#sidebar');
    const backdrop = $('#sidebarBackdrop');
    const menuBtn = $('#menuBtn');
    shell.setAttribute('data-sidebar', mode);

    const expanded = mode === 'expanded';
    if (menuBtn) menuBtn.setAttribute('aria-expanded', String(expanded));
    if (sidebar) {
      sidebar.inert = !expanded;
      if (expanded) sidebar.removeAttribute('aria-hidden');
      else sidebar.setAttribute('aria-hidden', 'true');
    }
    if (isNarrow()) {
      backdrop.hidden = !expanded;
      backdrop.setAttribute('data-visible', String(expanded));
      document.body.style.overflow = expanded ? 'hidden' : '';
    } else {
      backdrop.hidden = true;
      backdrop.setAttribute('data-visible', 'false');
      document.body.style.overflow = '';
    }
  }

  function toggleSidebar() {
    setSidebar($('#shell').getAttribute('data-sidebar') === 'expanded' ? 'collapsed' : 'expanded');
  }

  function applyTheme(theme) {
    const next = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    Store.setTheme(next);
    storage.set('isnad.gui.theme.v1', next);
    const use = $('#themeIcon use');
    if (use) use.setAttribute('href', next === 'dark' ? '#ic-sun' : '#ic-moon');
    const btn = $('#themeBtn');
    if (btn) btn.setAttribute('aria-label', next === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
  }

  function applyDirection(direction) {
    const next = direction === 'rtl' ? 'rtl' : 'ltr';
    document.documentElement.setAttribute('dir', next);
    Store.setSettings({ direction: next });
    Store.persist();
  }

  function exportConversation() {
    const record = Store.active();
    if (!record || !record.items.length) { Toasts.show('Nothing to export yet', 'error'); return; }

    const lines = ['# ' + record.title, ''];
    record.items.forEach(function (entry) {
      if (entry.role === 'user') {
        lines.push('**You**', '', entry.text || '', '');
      } else if (entry.kind === 'assistant') {
        lines.push('**ISNAD (' + (entry.model || 'model') + ')**', '');
        if (entry.text) lines.push(entry.text, '');
        (entry.citations || []).forEach(function (citation) {
          lines.push('### Citation · ' + (citation.header.reference || 'reference not given'));
          if (citation.state === 'done' && citation.report) {
            const report = citation.report;
            lines.push('', '- Status: `' + report.status + '` (' + statusInfo(report.status).label + ')');
            if (report.sourceMetadata) {
              lines.push('- Source: ' + report.sourceMetadata.name + ' v' + report.sourceMetadata.version +
                (report.sourceMetadata.content_sha256 ? ' · sha256 ' + report.sourceMetadata.content_sha256 : ''));
            }
            report.evidence.forEach(function (evidence) {
              lines.push('', '> ' + evidence.reference, '>', '> ' + evidence.source_text);
              if (evidence.attribution_text) lines.push('>', '> Attribution: ' + evidence.attribution_text);
              lines.push('>', '> Grade: ' + (evidence.grade_text
                ? evidence.grade_text + (evidence.grade_source ? ' (' + evidence.grade_source + ')' : '')
                : 'not supplied by the source'));
            });
            lines.push('', '_' + statusInfo(report.status).meaning + '_', '');
          } else if (citation.state === 'error' && citation.error) {
            lines.push('', '- Not checked: ' + citation.error.title + ' — ' + citation.error.message, '');
          }
        });
        lines.push('');
      } else if (entry.role === 'tool' && entry.kind === 'report') {
        const report = entry.report;
        lines.push('**Hand check**', '', '- Status: `' + report.status + '`', '');
        report.evidence.forEach(function (evidence) {
          lines.push('> ' + evidence.reference, '>', '> ' + evidence.source_text, '');
        });
      }
    });

    const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = (record.title || 'conversation').replace(/[^\w\u0600-\u06FF-]+/g, '-').slice(0, 40) + '.md';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    Toasts.show('Conversation exported', 'success');
  }

  /* ==========================================================================
     15. SETTINGS
     ======================================================================= */
  const PROVIDERS = {
    openai: { label: 'OpenAI', base: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
    openrouter: { label: 'OpenRouter', base: 'https://openrouter.ai/api/v1', model: '' },
    groq: { label: 'Groq', base: 'https://api.groq.com/openai/v1', model: '' },
    ollama: { label: 'Ollama (local)', base: 'http://127.0.0.1:11434/v1', model: 'llama3.1' },
    litellm: { label: 'LiteLLM proxy (local)', base: 'http://127.0.0.1:4000/v1', model: '' },
    custom: { label: 'Other OpenAI-compatible endpoint', base: '', model: '' }
  };

  function openSettings(tab) {
    const dialog = $('#settingsDialog');
    const settings = Store.state.settings;
    /* Called directly as a click handler the argument is an event, not a tab
       name, so only a known tab name is honoured. */
    const which = (tab === 'api' || tab === 'appearance') ? tab : 'model';
    $$('[data-settings-tab]').forEach(function (button) {
      button.setAttribute('aria-selected', String(button.dataset.settingsTab === which));
    });
    $$('[data-settings-panel]').forEach(function (panel) {
      panel.hidden = panel.dataset.settingsPanel !== which;
    });

    $('#providerSelect').value = detectProvider(settings.modelBase);
    $('#modelBaseInput').value = settings.modelBase || '';
    $('#modelNameInput').value = settings.modelName || '';
    $('#modelKeyInput').value = Store.modelKey() || '';
    $('#rememberKeyInput').checked = settings.modelRememberKey === true;
    $('#temperatureInput').value = String(settings.temperature);
    $('#apiBaseInput').value = settings.apiBase || '';
    $('#settingsError').hidden = true;

    const rtl = settings.direction === 'rtl';
    $$('[data-dir]').forEach(function (b) {
      b.setAttribute('aria-checked', String((b.dataset.dir === 'rtl') === rtl));
    });
    $$('[data-theme-opt]').forEach(function (b) {
      b.setAttribute('aria-checked', String(b.dataset.themeOpt === Store.state.theme));
    });
    $('#tsSwitch').setAttribute('aria-checked', String(settings.showTimestamps));
    $('#promptStatus').textContent = Store.state.systemPrompt
      ? 'Citation protocol ' + Store.state.systemPrompt.version + ' loaded and injected as the system message.'
      : 'The citation protocol prompt has not loaded, so chat is disabled. Refresh the connection.';

    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    $('#' + (which === 'model' ? 'modelBaseInput' : 'apiBaseInput')).focus();
  }

  function detectProvider(base) {
    const entry = Object.keys(PROVIDERS).find(function (key) {
      return PROVIDERS[key].base && PROVIDERS[key].base === base;
    });
    return entry || (base ? 'custom' : 'openai');
  }

  function closeSettings() {
    const dialog = $('#settingsDialog');
    if (typeof dialog.close === 'function' && dialog.open) dialog.close();
    else dialog.removeAttribute('open');
  }

  function saveSettings() {
    const error = $('#settingsError');
    let modelBase;
    let apiBase;
    try {
      modelBase = Model.normalizeBase($('#modelBaseInput').value);
    } catch (e) {
      return settingsError(error, e, 'model');
    }
    try {
      apiBase = Api.normalizeBase($('#apiBaseInput').value);
    } catch (e) {
      return settingsError(error, e, 'verification');
    }
    if (modelBase && !$('#modelNameInput').value.trim()) {
      error.textContent = 'Enter the model name the endpoint expects, for example gpt-4o-mini.';
      error.hidden = false;
      return;
    }

    const remember = $('#rememberKeyInput').checked;
    Store.setModelKey($('#modelKeyInput').value.trim(), remember);
    Store.setSettings({
      modelBase: modelBase,
      modelName: $('#modelNameInput').value.trim().slice(0, 120),
      modelRememberKey: remember,
      temperature: Math.min(2, Math.max(0, Number($('#temperatureInput').value) || 0)),
      apiBase: apiBase
    });
    Store.persist();
    error.hidden = true;
    closeSettings();
    Toasts.show(modelBase ? 'Model set to ' + Model.label() : 'Model disconnected', 'success');
    refreshApiState();
  }

  function settingsError(node, exception, which) {
    const reasons = {
      'not-a-url': 'Enter a full URL such as https://example.org, or leave the field blank.',
      'bad-scheme': 'Only http and https URLs are accepted.',
      'credentials-in-url': 'Remove the credentials from the URL. Keys belong in the key field, never in a URL.',
      'not-a-base': 'Give the base URL only, without a query string or fragment.'
    };
    node.textContent = (which === 'model' ? 'Model endpoint: ' : 'Verification API: ') +
      (reasons[exception.message] || 'That URL is not usable.');
    node.hidden = false;
  }

  function openPromptDialog() {
    const prompt = Store.state.systemPrompt;
    const body = prompt && prompt.prompt
      ? prompt.prompt
      : 'The citation protocol prompt has not loaded from the API.';
    const dialog = $('#promptDialog');
    $('#promptVersion').textContent = prompt ? prompt.version : 'not loaded';
    $('#promptText').textContent = body;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }

  /* ==========================================================================
     16. COMPOSER
     ======================================================================= */
  function syncComposer() {
    const input = $('#composerInput');
    const used = input.value.length;
    const limit = quoteLimit();
    const counter = $('#charCount');
    counter.textContent = formatCount(used) + ' / ' + formatCount(limit);
    $('.composer__meta').setAttribute('data-over-limit', String(used > limit));
    autoGrow(input);
    updateReferenceHint();
    updateSendState();
  }

  function updateReferenceHint() {
    const mode = Store.state.mode;
    const hint = $('#referenceHint');
    if (mode === 'chat') {
      hint.textContent = 'The model is asked to mark quotations with references; you can answer questions without one.';
      return;
    }
    const pair = supportedPair();
    const reference = $('#referenceInput').value.trim();
    if (!pair) {
      hint.textContent = 'No adapter is configured for the selected source and language.';
      $('#referenceInput').placeholder = 'reference';
      return;
    }
    hint.textContent = reference
      ? 'Reference format here: ' + pair.reference_format
      : 'Optional. With the locator you were given, the check is compared at that reference; without one, the wording is searched for in the whole source.';
    $('#referenceInput').placeholder = String(pair.reference_format || '').trim().slice(0, 40) || 'reference';
  }

  function applyMode(mode) {
    Store.setMode(mode);
    const verify = mode === 'verify';
    $('#verifyFields').hidden = !verify;
    $('#modeChat').setAttribute('aria-pressed', String(!verify));
    $('#modeVerify').setAttribute('aria-pressed', String(verify));
    $('#composerInput').placeholder = verify
      ? 'Paste the quoted source text…'
      : 'Ask anything… quotations will be checked before you see them';
    $('#sendBtnLabel').textContent = verify ? 'Verify' : 'Send';
    $('#sendIcon').innerHTML = '<use href="#' + (verify ? 'ic-check' : 'ic-send') + '"></use>';
    const prompts = Store.state.capabilities && Store.state.capabilities.chat_prompt_suggestions;
    Store.state.chatPrompts = prompts || [];
    syncComposer();
    renderItems({ scroll: false });
    scrollToBottom(true);
  }

  /* ==========================================================================
     17. SCROLL
     ======================================================================= */
  function scrollToBottom(instant) {
    const area = $('#scrollArea');
    if (!area) return;
    window.requestAnimationFrame(function () {
      area.scrollTo({ top: area.scrollHeight, behavior: instant ? 'auto' : 'smooth' });
    });
  }

  function updateScrollFade() {
    const area = $('#scrollArea');
    if (!area) return;
    area.setAttribute('data-scroll-top', String(area.scrollTop > 2));
    area.setAttribute('data-scroll-bottom', String(area.scrollTop + area.clientHeight < area.scrollHeight - 2));
  }

  /* ==========================================================================
     18. EVENT WIRING
     ======================================================================= */
  function wireEvents() {

    $('#composerForm').addEventListener('submit', function (event) {
      event.preventDefault();
      if (Store.state.mode === 'verify') sendVerifyRequest();
      else sendMessage();
    });

    const input = $('#composerInput');
    input.addEventListener('input', syncComposer);
    input.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' || event.isComposing) return;
      const verifyMode = Store.state.mode === 'verify';
      if (verifyMode && !event.shiftKey) {
        event.preventDefault();
        sendVerifyRequest();
        return;
      }
      if (!verifyMode && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });

    $('#referenceInput').addEventListener('input', function () { updateReferenceHint(); updateSendState(); });
    $('#sourceSelect').addEventListener('change', function () { updateReferenceHint(); updateSendState(); });
    $('#languageSelect').addEventListener('change', function () { updateReferenceHint(); updateSendState(); });

    $('#stopBtn').addEventListener('click', stopGeneration);

    $('#modeChat').addEventListener('click', function () { applyMode('chat'); });
    $('#modeVerify').addEventListener('click', function () { applyMode('verify'); });

    $('#newChatBtn').addEventListener('click', function () {
      Store.createChat();
      Store.persist();
      renderItems({ instant: true });
      $('#composerInput').value = '';
      autoGrow($('#composerInput'));
      updateSendState();
      if (isNarrow()) setSidebar('collapsed');
      $('#composerInput').focus();
    });

    const searchInput = $('#convSearch');
    const clearBtn = $('#clearSearchBtn');
    searchInput.addEventListener('input', function () {
      clearBtn.hidden = searchInput.value.length === 0;
      Store.setQuery(searchInput.value);
    });
    clearBtn.addEventListener('click', function () {
      searchInput.value = '';
      clearBtn.hidden = true;
      Store.setQuery('');
      searchInput.focus();
    });

    $('#themeBtn').addEventListener('click', function () {
      applyTheme(Store.state.theme === 'dark' ? 'light' : 'dark');
    });
    $('#exportBtn').addEventListener('click', exportConversation);
    $('#refreshApiBtn').addEventListener('click', function () {
      refreshApiState().then(function () { Toasts.show('Connection status refreshed', 'success'); });
    });
    $('#modelBadge').addEventListener('click', function () { openSettings('model'); });

    $('#settingsBtn').addEventListener('click', function () { openSettings('model'); });
    $('#closeSettingsBtn').addEventListener('click', closeSettings);
    $('#cancelSettingsBtn').addEventListener('click', closeSettings);
    $('#saveSettingsBtn').addEventListener('click', saveSettings);

    $$('[data-settings-tab]').forEach(function (button) {
      button.addEventListener('click', function () {
        const which = button.dataset.settingsTab;
        $$('[data-settings-tab]').forEach(function (other) {
          other.setAttribute('aria-selected', String(other === button));
        });
        $$('[data-settings-panel]').forEach(function (panel) {
          panel.hidden = panel.dataset.settingsPanel !== which;
        });
        $('#settingsError').hidden = true;
      });
    });

    $('#providerSelect').addEventListener('change', function () {
      const provider = PROVIDERS[$('#providerSelect').value];
      if (!provider) return;
      if (provider.base) $('#modelBaseInput').value = provider.base;
      if (provider.model && !$('#modelNameInput').value.trim()) $('#modelNameInput').value = provider.model;
    });

    $('#toggleKeyBtn').addEventListener('click', function () {
      const field = $('#modelKeyInput');
      const showing = field.type === 'text';
      field.type = showing ? 'password' : 'text';
      $('#toggleKeyBtn').textContent = showing ? 'Show' : 'Hide';
    });

    $('#closePromptBtn').addEventListener('click', function () {
      const dialog = $('#promptDialog');
      if (typeof dialog.close === 'function' && dialog.open) dialog.close();
      else dialog.removeAttribute('open');
    });

    $('#clearHistoryBtn').addEventListener('click', function () {
      if (!window.confirm('Delete every saved conversation from this browser? This cannot be undone.')) return;
      storage.remove('isnad.gui.state.v2');
      Store.state.chats = [];
      Store.state.activeId = null;
      Store.createChat();
      Store.persist();
      closeSettings();
      Toasts.show('Local history cleared', 'success');
    });

    $('#forgetKeyBtn').addEventListener('click', function () {
      Store.setModelKey('', false);
      Store.setSettings({ modelRememberKey: false });
      $('#modelKeyInput').value = '';
      $('#rememberKeyInput').checked = false;
      Store.persist();
      Toasts.show('Stored key forgotten', 'success');
    });

    $$('[data-theme-opt]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        applyTheme(btn.dataset.themeOpt);
        $$('[data-theme-opt]').forEach(function (b) {
          b.setAttribute('aria-checked', String(b.dataset.themeOpt === Store.state.theme));
        });
      });
    });

    $$('[data-dir]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        applyDirection(btn.dataset.dir);
        $$('[data-dir]').forEach(function (b) {
          b.setAttribute('aria-checked', String(b.dataset.dir === Store.state.settings.direction));
        });
      });
    });

    $('#tsSwitch').addEventListener('click', function () {
      const next = $('#tsSwitch').getAttribute('aria-checked') !== 'true';
      $('#tsSwitch').setAttribute('aria-checked', String(next));
      Store.setSettings({ showTimestamps: next });
      Store.persist();
    });

    $('#menuBtn').addEventListener('click', toggleSidebar);
    $('#collapseBtn').addEventListener('click', function () { setSidebar('collapsed'); });
    $('#sidebarBackdrop').addEventListener('click', function () { setSidebar('collapsed'); });

    const composerEl = $('.composer');
    const syncComposerHeight = function () {
      $('#surface').style.setProperty('--ui-composer-h', composerEl.offsetHeight + 'px');
    };
    syncComposerHeight();
    if (window.ResizeObserver) new ResizeObserver(syncComposerHeight).observe(composerEl);

    $('#scrollArea').addEventListener('scroll', updateScrollFade, { passive: true });

    $('#messages').addEventListener('click', function (event) {
      const btn = event.target.closest('[data-copy-code]');
      if (!btn) return;
      const block = btn.closest('.codeblock');
      const code = block ? $('.codeblock__pre code', block) : null;
      if (code) copyText(code.textContent);
    });

    document.addEventListener('keydown', function (event) {
      const mod = event.metaKey || event.ctrlKey;
      if (mod && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        $('#convSearch').focus();
        if (isNarrow()) setSidebar('expanded');
      } else if (mod && event.key.toLowerCase() === 'b') {
        event.preventDefault();
        toggleSidebar();
      } else if (mod && event.key.toLowerCase() === ',') {
        event.preventDefault();
        openSettings('model');
      } else if (event.key === 'Escape' && isNarrow() && $('#shell').getAttribute('data-sidebar') === 'expanded') {
        setSidebar('collapsed');
      }
    });

    let resizeTimer = 0;
    window.addEventListener('resize', function () {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(function () {
        if (isNarrow()) setSidebar($('#shell').getAttribute('data-sidebar') === 'expanded' ? 'expanded' : 'collapsed');
        else setSidebar($('#shell').getAttribute('data-sidebar') || 'expanded');
        updateScrollFade();
      }, 120);
    });

    window.addEventListener('beforeunload', function () {
      Store.persist();
      Speech.stop();
      const controller = Store.state.running.controller;
      if (controller) controller.abort();
    });
  }

  /* ==========================================================================
     19. RENDER SUBSCRIPTION
     ======================================================================= */
  function subscribeRender() {
    let scheduled = false;
    Store.subscribe(function () {
      if (scheduled) return;
      scheduled = true;
      window.requestAnimationFrame(function () {
        scheduled = false;

        renderChatList();
        renderConnection();
        updateReferenceHint();

        const record = Store.active();
        $('#convTitle').textContent = record ? record.title : 'New chat';

        const running = Store.state.running.active;
        $('#stopBtn').hidden = !running;
        updateSendState();

        /* A streaming reply is patched in place; the transcript is rebuilt only
           when its shape changed (a new item, a state change). */
        const container = $('#messages');
        const last = record && record.items.length ? record.items[record.items.length - 1] : null;
        const patched = running && Store.state.running.kind === 'chat' && last &&
          (last.kind === 'streaming') && patchStreamingItem(record.id, last.id);

        if (!patched) {
          const key = [
            Store.state.activeId, Store.state.mode,
            record ? record.items.length : 0,
            last ? last.kind + ':' + last.status + ':' + (last.citations ? last.citations.map(function (c) { return c.state; }).join(',') : '') : ''
          ].join('|');
          if (container.getAttribute('data-key') !== key) {
            container.setAttribute('data-key', key);
            renderItems({ scroll: false });
          }
        }
        updateScrollFade();
      });
    });
  }

  /* ==========================================================================
     20. BOOT
     ======================================================================= */
  function followPermalink() {
    const hash = window.location.hash || '';
    if (hash.indexOf('#m-') !== 0) return;
    const id = hash.slice(3);
    if (!id) return;
    window.requestAnimationFrame(function () {
      window.setTimeout(function () { highlightItem(id); }, 60);
    });
  }

  function init() {
    Store.load();
    applyTheme(Store.state.theme);
    document.documentElement.setAttribute('dir', Store.state.settings.direction === 'rtl' ? 'rtl' : 'ltr');

    subscribeRender();
    wireEvents();
    applyMode(Store.state.mode || 'chat');

    renderChatList();
    renderConnection();
    renderItems({ instant: true });
    syncComposer();
    updateScrollFade();
    setSidebar(isNarrow() ? 'collapsed' : 'expanded');

    const narrowQuery = window.matchMedia('(max-width: 900px)');
    if (narrowQuery.addEventListener) {
      narrowQuery.addEventListener('change', function (event) {
        setSidebar(event.matches ? 'collapsed' : 'expanded');
      });
    }

    if (window.matchMedia('(pointer: fine)').matches) {
      window.setTimeout(function () { $('#composerInput').focus(); }, 260);
    }

    followPermalink();
    window.addEventListener('hashchange', followPermalink);

    refreshApiState();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

})();
