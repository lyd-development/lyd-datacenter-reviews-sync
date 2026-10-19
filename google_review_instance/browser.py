"""Real Chrome + UA patching is required here since this app drives the same
Google Maps pages other scrapers in this project do — headless Chromium gets
served a degraded UI without it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright


async def launch_context(
    playwright: Playwright,
    *,
    headless: bool,
    proxy: dict | None = None,
    storage_state: dict[str, Any] | None = None,
) -> tuple[Browser, BrowserContext]:
    # Proxy auth handled manually below via the CDP Fetch domain (see
    # attach_proxy_auth) instead of Playwright's built-in proxy={username,
    # password} — confirmed live (2026-10-14, same root cause documented in
    # the sibling lyd-datacenter-reviews-tripadvisor project's FINDINGS.md)
    # that passing credentials straight into Playwright/Chromium's own proxy
    # option is unreliable with Apify's Residential proxy and causes
    # page.goto() to hang/timeout even though the proxy itself works fine
    # (confirmed separately via plain curl). Only the bare server is passed
    # to the browser; the CDP Fetch domain intercepts the 407 challenge and
    # supplies credentials instead.
    launch_args = [f"--proxy-server={proxy['server']}"] if proxy else []
    browser = await playwright.chromium.launch(headless=headless, channel="chrome", args=launch_args)

    probe_page = await browser.new_page()
    real_user_agent = await probe_page.evaluate("() => navigator.userAgent")
    await probe_page.close()
    user_agent = real_user_agent.replace("HeadlessChrome", "Chrome")

    # storage_state (cookies from a manually-logged-in Google session, see
    # main.py) — confirmed live (2026-10-14) that an anonymous/logged-out
    # session gets a "Sign-in to get the best of Google Maps" interstitial
    # that intercepts the Sort-by-Newest click specifically (the dropdown
    # never opens, menuitemradio never attaches), while the exact same click
    # on a logged-in session never triggers that modal at all. Automated
    # login via Playwright is not an option — Google rejects it outright
    # ("This browser or app may not be secure") before even reaching the
    # password step — so the session has to come from a real, manually
    # logged-in browser instead.
    context = await browser.new_context(
        locale="en-US",
        timezone_id="UTC",
        viewport={"width": 1920, "height": 1080},
        user_agent=user_agent,
        storage_state=storage_state,
    )
    await context.add_init_script(
        """() => {
            Object.defineProperty(navigator, "webdriver", { get: () => undefined });
            Object.defineProperty(navigator, "plugins", { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, "languages", { get: () => ["en-US", "en"] });
            window.chrome = { runtime: {} };
        }"""
    )
    # Raised 30s -> 60s (2026-10-14) for Residential proxy runs — confirmed
    # live that the proxy itself connects and loads google.com fine in 3-6s
    # standalone (plain curl, both with and without a sticky session), so a
    # bare "page.goto() timed out" here is most likely Residential's added
    # per-request latency (slower/flakier than a direct or datacenter-proxy
    # connection) stacking with normal browser startup overhead, not a
    # broken proxy config.
    context.set_default_navigation_timeout(60_000)
    return browser, context


async def attach_proxy_auth(context: BrowserContext, page: Page, proxy: dict | None) -> None:
    """Wires up proxy Basic-auth via the CDP Fetch domain — must be called
    once per page, before that page navigates anywhere, whenever `proxy` is
    set. No-op if `proxy` is None (no proxy in use for this run). See
    launch_context's comment above for why this exists instead of just
    passing username/password into Playwright's own proxy option."""
    if not proxy:
        return

    cdp = await context.new_cdp_session(page)
    await cdp.send("Fetch.enable", {"handleAuthRequests": True})

    async def on_auth_required(event: dict) -> None:
        await cdp.send(
            "Fetch.continueWithAuth",
            {
                "requestId": event["requestId"],
                "authChallengeResponse": {
                    "response": "ProvideCredentials",
                    "username": proxy.get("username", ""),
                    "password": proxy.get("password", ""),
                },
            },
        )

    async def on_request_paused(event: dict) -> None:
        try:
            await cdp.send("Fetch.continueRequest", {"requestId": event["requestId"]})
        except Exception:
            pass  # request may have already resolved/aborted — not actionable here

    # Playwright's CDPSession.on() callbacks run synchronously — wrap the
    # async handlers in a fire-and-forget task on the running event loop.
    cdp.on("Fetch.authRequired", lambda event: asyncio.ensure_future(on_auth_required(event)))
    cdp.on("Fetch.requestPaused", lambda event: asyncio.ensure_future(on_request_paused(event)))
