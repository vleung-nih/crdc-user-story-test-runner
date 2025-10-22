import asyncio
import os
import re
import urllib.parse
from pathlib import Path

import pyotp
from playwright.async_api import async_playwright


ALLOWED_AUTH_SUFFIXES = [
    "nih.gov",
    "authtest.nih.gov",
    "stsstg.nih.gov",
    "login.gov",
    "identitysandbox.gov",
]


def host_allowed(url: str, base_host: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return True
    if not host:
        return True
    if host == base_host or host.endswith("." + base_host):
        return True
    return any(host == suf or host.endswith("." + suf) for suf in ALLOWED_AUTH_SUFFIXES)


async def consent_dismiss(page, verbose: bool = False) -> None:
    patterns = [r"continue", r"ok", r"accept", r"i\s*agree", r"proceed"]
    for pat in patterns:
        try:
            btn = page.get_by_role("button", name=re.compile(pat, re.I)).first
            if await btn.is_visible():
                if verbose:
                    print(f"→ Dismissing consent button /{pat}/i")
                await btn.click(timeout=5000)
                await page.wait_for_load_state("networkidle")
                return
        except Exception:
            pass
        # Only click links if they are clearly inside a modal/dialog and same-origin/allowlisted
        try:
            lnk = page.get_by_role("link", name=re.compile(pat, re.I)).first
            if await lnk.is_visible():
                # Must be inside a dialog-like container
                in_dialog = False
                try:
                    in_dialog = await lnk.evaluate(
                        "el => !!el.closest('[role=dialog],[aria-modal=true],.modal,.cookie,.consent,.cookie-banner,.cc-window')"
                    )
                except Exception:
                    in_dialog = False
                href = ""
                try:
                    href = await lnk.get_attribute("href") or ""
                except Exception:
                    href = ""
                same_or_allowed = True
                if href:
                    same_or_allowed = host_allowed(href, urllib.parse.urlparse(page.url).hostname or "")
                if in_dialog and same_or_allowed:
                    if verbose:
                        dest = href or "(no href)"
                        print(f"→ Dismissing consent link /{pat}/i inside dialog to {dest}")
                    await lnk.click(timeout=5000)
                    await page.wait_for_load_state("networkidle")
                    return
        except Exception:
            pass


async def click_login_button(page, verbose: bool = False) -> None:
    # Prefer explicit test id
    try:
        await page.wait_for_selector("[data-testid='login-button']", state="visible", timeout=6000)
        await page.click("[data-testid='login-button']", timeout=6000)
        if verbose:
            print("✓ Clicked [data-testid='login-button']")
        return
    except Exception:
        pass

    # Fallback: role and text, avoid social providers
    disallow = re.compile(r"facebook|google|github|twitter|apple|orcid|microsoft|azure|linkedin", re.I)
    for role in ("button", "link"):
        try:
            # Match variations but exclude social providers and external links
            loc = page.get_by_role(role, name=re.compile(r"^(login|log\s*in|sign\s*in)$", re.I)).first
            if await loc.is_visible():
                name = (await loc.inner_text()) or ""
                if disallow.search(name):
                    raise Exception("Filtered social provider control")
                # If it's a link, ensure it's same-origin or allowlisted
                if role == "link":
                    href = await loc.get_attribute("href") or ""
                    if href and not host_allowed(href, urllib.parse.urlparse(page.url).hostname or ""):
                        raise Exception("Filtered external login link")
                if verbose:
                    print(f"→ Clicking {role} exact name Login/Sign in")
                await loc.click(timeout=6000)
                await page.wait_for_load_state("networkidle")
                return
        except Exception:
            pass
    raise AssertionError("Login button not found")


async def click_login_gov(page, verbose: bool = False) -> None:
    for role in ("button", "link"):
        try:
            loc = page.get_by_role(role, name=re.compile(r"login\.gov", re.I)).first
            if await loc.is_visible():
                if verbose:
                    print(f"→ Clicking {role} Login.gov")
                await loc.click(timeout=6000)
                await page.wait_for_load_state("networkidle")
                return
        except Exception:
            pass
    # Fallback text selector
    try:
        await page.get_by_text("Login.gov", exact=False).first.click(timeout=6000)
        await page.wait_for_load_state("networkidle")
        if verbose:
            print("→ Clicked Login.gov via text")
        return
    except Exception:
        pass
    raise AssertionError("Login.gov button not found")


async def fill_credentials_and_submit(page, username: str, password: str, verbose: bool = False) -> None:
    # Prefer labels first (works on login.gov)
    try:
        await page.get_by_label(re.compile(r"(email|username)", re.I)).first.fill(username, timeout=8000)
        if verbose:
            print("→ Filled username by label")
    except Exception:
        # Fallback common selectors
        for sel in [
            "input[type='email']",
            "#email",
            "#username",
            "input[name='email']",
            "input[name='username']",
        ]:
            try:
                await page.fill(sel, username, timeout=5000)
                if verbose:
                    print(f"→ Filled username via {sel}")
                break
            except Exception:
                continue
        else:
            raise AssertionError("Unable to fill username/email")

    try:
        await page.get_by_label(re.compile(r"password", re.I)).first.fill(password, timeout=8000)
        if verbose:
            print("→ Filled password by label")
    except Exception:
        for sel in [
            "input[type='password']",
            "#password",
            "input[name='password']",
        ]:
            try:
                await page.fill(sel, password, timeout=5000)
                if verbose:
                    print(f"→ Filled password via {sel}")
                break
            except Exception:
                continue
        else:
            raise AssertionError("Unable to fill password")

    # Submit
    for role in ("button",):
        try:
            loc = page.get_by_role(role, name=re.compile(r"^(sign\s*in|continue|submit)$", re.I)).first
            if await loc.is_visible():
                await loc.click(timeout=6000)
                await page.wait_for_load_state("networkidle")
                if verbose:
                    print("→ Clicked Sign in")
                return
        except Exception:
            pass
    for sel in ["button[type='submit']", "#submit"]:
        try:
            await page.click(sel, timeout=6000)
            await page.wait_for_load_state("networkidle")
            if verbose:
                print(f"→ Clicked submit via {sel}")
            return
        except Exception:
            continue
    raise AssertionError("Unable to submit credentials")


async def handle_otp_and_consent(page, totp_secret: str, base_host: str, verbose: bool = False) -> None:
    for attempt in range(2):
        code = pyotp.TOTP(totp_secret).now()
        if verbose:
            print(f"→ OTP attempt {attempt+1}, code={code}")

        # Fill OTP by label or common selectors
        try:
            await page.get_by_label(re.compile(r"(one[- ]?time|verification|auth|otp).*code", re.I)).first.fill(code, timeout=8000)
        except Exception:
            filled = False
            for sel in ["#otp", "input[name*='otp']", "input[id*='otp']", "input[name*='code']", "input[id*='code']"]:
                try:
                    await page.fill(sel, code, timeout=5000)
                    filled = True
                    break
                except Exception:
                    continue
            if not filled:
                if attempt == 1:
                    raise AssertionError("Unable to fill OTP code")
                await page.wait_for_timeout(30000)
                continue

        # Submit OTP
        try:
            await page.get_by_role("button", name=re.compile(r"^(submit|continue|verify|sign\s*in)$", re.I)).first.click(timeout=6000)
        except Exception:
            for sel in ["button[type='submit']", "#submit"]:
                try:
                    await page.click(sel, timeout=6000)
                    break
                except Exception:
                    continue

        # Consent grant if shown (search across frames, multiple labels)
        grant_labels = re.compile(r"\b(grant|authorize|allow|approve|consent|agree)\b", re.I)
        async def try_click_grant_anywhere() -> bool:
            # Frames to search: main frame + all child frames
            frames = []
            try:
                mainf = page.main_frame
                if mainf:
                    frames.append(mainf)
            except Exception:
                pass
            for fr in page.frames:
                if fr not in frames:
                    frames.append(fr)
            # Try role=button/link by accessible name
            for fr in frames:
                try:
                    loc = fr.get_by_role("button", name=grant_labels).first
                    if await loc.is_visible():
                        if verbose:
                            print(f"→ Clicking consent button in frame {fr.url}")
                        await loc.click(timeout=6000)
                        await fr.wait_for_load_state("networkidle")
                        return True
                except Exception:
                    pass
                try:
                    loc = fr.get_by_role("link", name=grant_labels).first
                    if await loc.is_visible():
                        if verbose:
                            print(f"→ Clicking consent link in frame {fr.url}")
                        await loc.click(timeout=6000)
                        await fr.wait_for_load_state("networkidle")
                        return True
                except Exception:
                    pass
                # Try generic selectors
                for sel in [
                    "button:has-text('Grant')",
                    "button:has-text('Authorize')",
                    "button:has-text('Allow')",
                    "button:has-text('Approve')",
                    "button:has-text('Consent')",
                    "a:has-text('Grant')",
                    "a:has-text('Authorize')",
                    "a:has-text('Allow')",
                    "a:has-text('Approve')",
                    "a:has-text('Consent')",
                ]:
                    try:
                        el = await fr.query_selector(sel)
                        if el and await el.is_visible():
                            if verbose:
                                print(f"→ Clicking consent via selector in frame {fr.url}: {sel}")
                            await el.click(timeout=6000)
                            await fr.wait_for_load_state("networkidle")
                            return True
                    except Exception:
                        continue
            return False

        # Try to click grant for up to ~8 seconds
        end_grant = page.context._impl_obj._loop.time() + 8
        while page.context._impl_obj._loop.time() < end_grant:
            if await try_click_grant_anywhere():
                return
            await page.wait_for_timeout(250)

        # Or success by redirect back to hub
        end = page.context._impl_obj._loop.time() + 10
        while page.context._impl_obj._loop.time() < end:
            if (urllib.parse.urlparse(page.url).hostname or "").endswith(base_host):
                return
            await page.wait_for_timeout(250)

        await page.wait_for_timeout(30000)

    raise AssertionError("OTP failed after 2 attempts")


async def run_test_suite(base_url: str, test_cases: list[dict], run_dir: Path, headless: bool = True, verbose: bool = False) -> dict:
    screenshots_dir = run_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1366, "height": 900})
        page = await context.new_page()

        base_host = urllib.parse.urlparse(base_url).hostname or ""

        # Block disallowed domains
        async def route_guard(route, request):
            try:
                if request.resource_type == "document" and request.is_navigation_request():
                    if not host_allowed(request.url, base_host):
                        if verbose:
                            print(f"⛔ Blocking navigation: {request.url}")
                        await route.abort()
                        return
            except Exception:
                pass
            await route.continue_()

        await context.route("**/*", route_guard)

        # Close any disallowed popups
        async def on_popup(popup_page):
            try:
                await popup_page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            if not host_allowed(popup_page.url, base_host):
                if verbose:
                    print(f"⛔ Closing popup: {popup_page.url}")
                await popup_page.close()

        page.on("popup", lambda p: asyncio.create_task(on_popup(p)))

        results = []

        for test in test_cases:
            if verbose:
                print(f"\n===== Running Test: {test.get('name','Unnamed')} =====")
            status = "passed"
            error = ""
            screenshot = ""
            try:
                for step in test.get("steps", []):
                    if verbose:
                        print(f"→ Step: {step}")
                    action = step.get("action")
                    if action == "navigate":
                        url = step.get("url", "/")
                        target = url if url.startswith("http") else base_url.rstrip("/") + "/" + url.lstrip("/")
                        await page.goto(target, timeout=60000)
                        await page.wait_for_load_state("networkidle")
                        await consent_dismiss(page, verbose=verbose)
                    elif action == "login_via_login_gov":
                        username = os.environ.get(step.get("username_env", "LOGIN_USERNAME"), "")
                        password = os.environ.get(step.get("password_env", "LOGIN_PASSWORD"), "")
                        secret = os.environ.get(step.get("totp_env", "TOTP_SECRET"), "")
                        if not username or not password or not secret:
                            raise AssertionError("Missing LOGIN_USERNAME/LOGIN_PASSWORD/TOTP_SECRET envs")
                        # Go to base page and consent
                        await page.goto(base_url, timeout=60000)
                        await page.wait_for_load_state("networkidle")
                        await consent_dismiss(page, verbose=verbose)
                        await click_login_button(page, verbose=verbose)
                        await click_login_gov(page, verbose=verbose)
                        await fill_credentials_and_submit(page, username, password, verbose=verbose)
                        await handle_otp_and_consent(page, secret, (urllib.parse.urlparse(base_url).hostname or ""), verbose=verbose)
                    elif action in ("assert_text",):
                        text = step.get("text")
                        loc = page.get_by_text(text, exact=False).first
                        await loc.wait_for(state="visible", timeout=8000)
                    elif action in ("assert_element_present", "assert_element_presence", "assert_element_exists"):
                        selector = step.get("selector")
                        timeout_ms = 8000
                        if selector and selector.startswith("text="):
                            text = selector.split("=", 1)[1]
                            await page.get_by_text(text, exact=False).first.wait_for(state="visible", timeout=timeout_ms)
                        elif step.get("text") and selector in ("button", "link"):
                            role = selector
                            name = step.get("text")
                            await page.get_by_role(role, name=re.compile(re.escape(name), re.I)).first.wait_for(state="visible", timeout=timeout_ms)
                        else:
                            await page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
                    elif action == "click":
                        selector = step.get("selector")
                        timeout_ms = 10000
                        if selector and selector.startswith("text="):
                            text = selector.split("=", 1)[1]
                            await page.get_by_text(text, exact=False).first.click(timeout=timeout_ms)
                        elif step.get("text") and selector in ("button", "link"):
                            role = selector
                            name = step.get("text")
                            loc = page.get_by_role(role, name=re.compile(re.escape(name), re.I)).first
                            # For links, verify allowlisted host
                            if role == "link":
                                href = await loc.get_attribute("href") or ""
                                if href and not host_allowed(href, urllib.parse.urlparse(page.url).hostname or ""):
                                    raise AssertionError(f"Blocked click to external link: {href}")
                            await loc.click(timeout=timeout_ms)
                        else:
                            await page.click(selector, timeout=timeout_ms)
                        await page.wait_for_load_state("networkidle")
                    elif action in ("screenshot",):
                        name = step.get("name", test.get("name", "screenshot").replace(" ", "_").lower())
                        shot = screenshots_dir / f"{name}.png"
                        await page.screenshot(path=str(shot), full_page=True)
                        screenshot = str(shot)
                    else:
                        # Unknown action
                        raise AssertionError(f"Unknown action in rewritten runner: {action}")
            except Exception as e:
                status = "failed"
                error = str(e)
                # Always print an error line to console
                current_url = ""
                try:
                    current_url = page.url
                except Exception:
                    current_url = ""
                print(f"✖ Test failed: {test.get('name','Unnamed')} — {error} (url={current_url})")
                try:
                    shot = screenshots_dir / (test.get("name", "failure").replace(" ", "_").lower() + "_failure.png")
                    await page.screenshot(path=str(shot), full_page=True)
                    screenshot = str(shot)
                except Exception:
                    pass

            results.append({
                "name": test.get("name", "Unnamed"),
                "status": status,
                "error": error,
                "screenshot": screenshot,
                "steps": test.get("steps", []),
            })
            # Print per-test summary to console
            if status == "passed":
                print(f"✓ Passed: {test.get('name','Unnamed')}")
            else:
                # Trim error for readability
                err_excerpt = error if len(error) < 300 else (error[:297] + "...")
                print(f"✖ Failed: {test.get('name','Unnamed')} — {err_excerpt}")

        await browser.close()
        return {"tests": results}


