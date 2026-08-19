"""Real Chrome + UA patching is required here since this app drives the same
Google Maps pages other scrapers in this project do — headless Chromium gets
served a degraded UI without it.
"""

from __future__ import annotations

from playwright.async_api import Browser, BrowserContext, Playwright


async def launch_context(
    playwright: Playwright, *, headless: bool, proxy: dict | None = None
) -> tuple[Browser, BrowserContext]:
    browser = await playwright.chromium.launch(headless=headless, channel="chrome", proxy=proxy)

    probe_page = await browser.new_page()
    real_user_agent = await probe_page.evaluate("() => navigator.userAgent")
    await probe_page.close()
    user_agent = real_user_agent.replace("HeadlessChrome", "Chrome")

    context = await browser.new_context(
        locale="en-US",
        timezone_id="UTC",
        viewport={"width": 1920, "height": 1080},
        user_agent=user_agent,
    )
    await context.add_init_script(
        """() => {
            Object.defineProperty(navigator, "webdriver", { get: () => undefined });
            Object.defineProperty(navigator, "plugins", { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, "languages", { get: () => ["en-US", "en"] });
            window.chrome = { runtime: {} };
        }"""
    )
    context.set_default_navigation_timeout(30_000)
    return browser, context
