"""Browser smoke tests for the served interface, skipped where no browser is installed.

The rest of the suite checks the interface's markup and text. These drive the
real page against the real app — and, for the chat surface, against a scripted
OpenAI-compatible endpoint — so that a broken selector, a policy that blocks the
script, a leaked quotation, or a misplaced drawer is caught here rather than by
hand.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from isnad_core.api.app import create_app

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

_PROVIDER_PATH = Path(__file__).resolve().parents[1] / "examples" / "fake_openai_provider.py"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    """Serve the real app on a loopback port for the duration of the module."""

    port = _free_port()
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.skip("the API server did not start in time")
    yield f"http://127.0.0.1:{port}/"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def scripted_model_url() -> Iterator[str]:
    """Serve the scripted model double used by the chat test."""

    spec = importlib.util.spec_from_file_location("isnad_fake_provider_browser", _PROVIDER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    port = _free_port()
    server = module.ThreadingHTTPServer(("127.0.0.1", port), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/v1"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():  # noqa: ANN201 - playwright types are optional dependencies
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch()
        except Error as exc:  # pragma: no cover - depends on the host image
            pytest.skip(f"chromium is not available for playwright: {exc}")
        yield instance
        instance.close()


def _seed_storage(target, **pairs: object) -> None:
    """Write localStorage before any page script runs, the only reliable way.

    The interface stores a value by JSON-encoding it once, so a JS string
    literal holding the JSON text of the value reproduces its own writes. The
    previous build's settings are seeded the same way, which is also how the
    migration test replays what that build left behind.
    """

    script = ";".join(
        f"window.localStorage.setItem({json.dumps(key)}, {json.dumps(json.dumps(value))})"
        for key, value in pairs.items()
    )
    target.add_init_script(script + ";")


def test_interface_reports_connection_and_verifies_by_hand_without_console_errors(
    server_url, browser
) -> None:
    page = browser.new_page(viewport={"width": 1400, "height": 950})
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(server_url, wait_until="load")
    page.wait_for_selector('#apiBadge[data-state="ready"]', timeout=20000)
    assert "API READY" in page.inner_text("#apiBadge")

    # The page opens on the chat surface, and says plainly that no model is set up.
    assert page.locator(".empty__title").inner_text() == "Connect a model to chat"
    assert "NO MODEL" in page.inner_text("#modelBadge")

    # The hand-check surface keeps working without any model at all.
    page.click("#modeVerify")
    page.wait_for_selector("#verifyFields:not([hidden])")
    assert page.locator(".empty__title").inner_text() == "Check a quotation by hand"

    quote = "بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ"
    page.select_option("#languageSelect", "ar")
    page.fill("#composerInput", quote)
    page.fill("#referenceInput", "1:1")
    page.click("#sendBtn")
    # The pending panel also renders a status chip, so wait for the resolved
    # verdict, which is the one carrying the raw status code.
    page.wait_for_selector(".result__status code", timeout=30000)
    verdict = page.locator(".result__status").last.inner_text()
    assert "normalized_match" in verdict
    # The submitted quote is echoed as submitted, and the source wording is
    # shown next to it rather than replacing it.
    assert quote in page.locator(".result__grid").last.inner_text()
    source_text = page.locator(".evidence__text").last.inner_text()
    assert len(source_text) > 10
    assert page.locator('.diff__text[data-side="source"]').count() >= 1

    # Switching back keeps both surfaces on one page.
    page.click("#modeChat")
    page.wait_for_selector("#verifyFields", state="hidden")

    page.close()
    assert errors == []


def test_chat_holds_quotations_until_the_source_answers(
    server_url, scripted_model_url, browser
) -> None:
    context = browser.new_context(viewport={"width": 1400, "height": 950})
    settings = {
        "apiBase": "",
        "modelBase": scripted_model_url,
        "modelName": "fake-citation-model",
        "modelRememberKey": True,
        "temperature": 0.0,
        "direction": "ltr",
        "showTimestamps": True,
    }
    _seed_storage(
        context,
        **{"isnad.gui.settings.v2": settings, "isnad.gui.modelkey.v1": "test-key"},
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(server_url, wait_until="load")
    page.wait_for_selector('#apiBadge[data-state="ready"]', timeout=20000)
    # The badge is uppercased for display, so compare without case.
    assert "fake-citation-model" in page.inner_text("#modelBadge").lower()
    assert page.locator(".empty__title").inner_text() == "Ask anything"

    page.fill("#composerInput", "Quote Sūrat al-Ikhlāṣ.")
    page.click("#sendBtn")

    # Ordinary prose is on screen while the answer is still arriving.
    page.wait_for_function(
        "() => { const el = document.querySelector('article.msg--assistant .rich');"
        " return el && el.innerText.includes('Here is a short answer'); }",
        timeout=30000,
    )

    # Each marked quotation becomes its own card, and only after the API has
    # answered: the raw status code is rendered from the verification result.
    page.wait_for_function(
        "() => document.querySelectorAll('.citation code').length === 2", timeout=60000
    )
    cards = page.locator(".citation")
    assert cards.count() == 2
    first = cards.nth(0).inner_text()
    assert "normalized_match" in first
    assert "112:1" in first
    # The source wording is shown verbatim, next to the submitted text.
    assert "He is Allāh" in page.locator(".citation .evidence__text").first.inner_text()

    second = cards.nth(1).inner_text()
    assert "mismatch" in second or "not_found" in second

    # A quotation the pinned corpus does not contain is echoed only as the text
    # that was submitted, never as source wording: it may appear in the quoted
    # text row and on the submitted side of a difference, and nowhere else.
    assert "Everlasting Guardian" in second
    assert "QUOTED TEXT" in second
    for index in range(page.locator(".evidence").count()):
        assert "Everlasting Guardian" not in page.locator(".evidence").nth(index).inner_text()
    for index in range(page.locator('.diff__text[data-side="source"]').count()):
        assert (
            "Everlasting Guardian"
            not in page.locator('.diff__text[data-side="source"]').nth(index).inner_text()
        )

    assert errors == []
    page.close()
    context.close()


def test_settings_dialog_opens_on_a_visible_panel(server_url, browser) -> None:
    page = browser.new_page(viewport={"width": 1400, "height": 950})
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(server_url, wait_until="load")
    page.wait_for_selector('#apiBadge[data-state="ready"]', timeout=20000)

    # A dialog whose panels are all hidden looks like an empty box: the fields of
    # the panel selected on open must be on screen.
    page.click("#settingsBtn")
    page.wait_for_selector("#settingsDialog[open]")
    assert page.locator("#providerSelect").is_visible()
    assert page.locator("#modelBaseInput").is_visible()
    assert page.locator("#modelKeyInput").get_attribute("type") == "password"
    assert not page.locator("#apiBaseInput").is_visible()
    assert not page.locator('[data-settings-panel="appearance"]').is_visible()

    page.click('[data-settings-tab="api"]')
    assert page.locator("#apiBaseInput").is_visible()
    assert not page.locator("#providerSelect").is_visible()

    page.click('[data-settings-tab="appearance"]')
    assert page.locator("#tsSwitch").is_visible()
    assert page.locator('[data-dir="rtl"]').is_visible()

    # Exactly one panel is shown at a time, whichever tab is selected.
    visible_panels = page.evaluate(
        "() => Array.from(document.querySelectorAll('[data-settings-panel]'))"
        ".filter(p => !p.hasAttribute('hidden')).map(p => p.dataset.settingsPanel)"
    )
    assert visible_panels == ["appearance"]
    selected = page.evaluate(
        "() => Array.from(document.querySelectorAll('[data-settings-tab]'))"
        ".filter(b => b.getAttribute('aria-selected') === 'true')"
        ".map(b => b.dataset.settingsTab)"
    )
    assert selected == ["appearance"]

    page.click("#cancelSettingsBtn3")
    page.wait_for_selector("#settingsDialog", state="hidden")
    assert errors == []
    page.close()


def test_interface_states_that_a_source_failure_decided_nothing(server_url, browser) -> None:
    # A fresh context with the base URL seeded before any page script runs: the
    # interface persists its own settings on unload, so writing them from a live
    # page would be overwritten by that page's own save. The key is the one the
    # previous build wrote, which the interface still reads.
    context = browser.new_context(viewport={"width": 1400, "height": 950})
    settings = {"apiBase": "http://127.0.0.1:8123", "direction": "ltr", "showTimestamps": True}
    _seed_storage(context, **{"isnad.gui.settings.v1": settings})
    page = context.new_page()

    page.goto(server_url, wait_until="load")
    page.wait_for_selector('#apiBadge[data-state="unavailable"]', timeout=20000)

    page.click("#modeVerify")
    page.wait_for_selector("#verifyFields:not([hidden])")
    page.select_option("#sourceSelect", "quran")
    page.select_option("#languageSelect", "en")
    page.fill("#composerInput", "Say: He is Allah, the One and Only.")
    page.fill("#referenceInput", "112:1")
    page.click("#sendBtn")
    page.wait_for_selector(".result--error", timeout=30000)

    body = page.locator(".result--error").last.inner_text()
    assert "unreachable" in body.lower()
    assert "not a not-found result" in body
    # A failed check must not be reported as a match status of any kind.
    for status in ("not_found_in_checked_corpus", "exact_match", "mismatch_at_cited_reference"):
        assert status not in body

    page.close()
    context.close()


def test_narrow_layout_keeps_the_drawer_above_its_scrim(server_url, browser) -> None:
    page = browser.new_page(viewport={"width": 430, "height": 860})

    page.goto(server_url, wait_until="load")
    page.wait_for_selector("#menuBtn")
    page.click("#menuBtn")
    page.wait_for_timeout(300)

    # The menu button that opened the drawer must itself be clickable again,
    # which it is not if the scrim paints over the sidebar.
    assert page.locator("#shell").get_attribute("data-sidebar") == "expanded"
    page.click("#newChatBtn")
    page.wait_for_timeout(250)
    assert page.locator("#shell").get_attribute("data-sidebar") == "collapsed"

    page.close()
