import asyncio
import json
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

# Default selector overrides (self-healing hints)
DEFAULT_OVERRIDES = {
    "user-menu-toggle": [
        {"engine": "role", "role": "button", "name_regex": r"user|account|profile"},
        {"engine": "text", "text": "User"},
        {"engine": "css", "value": "[aria-label*='User']"},
    ],
    "user-account-menu": [
        {"engine": "role", "role": "button", "name_regex": r"user|account|profile"},
    ],
    "manage-studies-link": [
        {"engine": "role", "role": "menuitem", "name_regex": r"manage\s*studies"},
        {"engine": "text", "text": "Manage Studies"},
        {"engine": "css", "value": "a:has-text('Manage Studies')"},
    ],
    "studies-list": [
        {"engine": "role", "role": "table", "name_regex": r"stud(y|ies)"},
        {"engine": "role", "role": "list", "name_regex": r"stud(y|ies)"},
        {"engine": "text", "text": "Studies"},
    ],
    "add-study-button": [
        {"engine": "role", "role": "button", "name_regex": r"add\s*study"},
        {"engine": "text", "text": "Add Study"},
        {"engine": "css", "value": "button:has-text('Add Study')"},
        {"engine": "css", "value": "[aria-label*='Add Study']"},
    ],
}


async def build_element_inventory(page, limit: int = 200) -> dict:
    """Collect lightweight element inventory to aid selector repair and agent context."""
    inventory: dict[str, list] = {
        "testids": [],
        "aria_labels": [],
        "buttons": [],
        "links": [],
        "menuitems": [],
        "roles": [],
    }
    try:
        # data-testid values
        testid_els = await page.query_selector_all("[data-testid]")
        for el in testid_els[:limit]:
            try:
                v = await el.get_attribute("data-testid")
                if v and v not in inventory["testids"]:
                    inventory["testids"].append(v)
            except Exception:
                continue
    except Exception:
        pass
    try:
        aria_els = await page.query_selector_all("[aria-label]")
        for el in aria_els[:limit]:
            try:
                v = await el.get_attribute("aria-label")
                if v and v not in inventory["aria_labels"]:
                    inventory["aria_labels"].append(v)
            except Exception:
                continue
    except Exception:
        pass
    # Role-based text
    async def collect_role(role: str, key: str):
        try:
            els = await page.query_selector_all(f"[role='{role}']")
            for el in els[:limit]:
                try:
                    txt = (await el.inner_text()).strip()
                    if txt and txt not in inventory[key]:
                        inventory[key].append(txt)
                except Exception:
                    continue
            if role not in inventory["roles"]:
                inventory["roles"].append(role)
        except Exception:
            pass
    await collect_role("button", "buttons")
    await collect_role("link", "links")
    await collect_role("menuitem", "menuitems")
    return inventory


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


async def login_button_visible(page) -> bool:
    """Return True if a Login/Log in/Sign in control is visible on the current page."""
    # Prefer explicit test id
    try:
        el = await page.query_selector("[data-testid='login-button']")
        if el and await el.is_visible():
            return True
    except Exception:
        pass
    # Role/button patterns
    try:
        loc = page.get_by_role("button", name=re.compile(r"^(login|log\s*in|sign\s*in)$", re.I)).first
        if await loc.is_visible():
            return True
    except Exception:
        pass
    # Text-based fallback
    try:
        loc = page.get_by_text(re.compile(r"^(login|log\s*in|sign\s*in)$", re.I), exact=False).first
        if await loc.is_visible():
            return True
    except Exception:
        pass
    return False


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
        filled = False
        try:
            if verbose:
                print(f"→ Attempting to fill OTP by label")
            await page.get_by_label(re.compile(r"(one[- ]?time|verification|auth|otp).*code", re.I)).first.fill(code, timeout=8000)
            filled = True
            if verbose:
                print(f"✓ OTP filled via label")
        except Exception as e:
            if verbose:
                print(f"→ OTP fill by label failed: {e}, trying fallback selectors")
            for sel in ["#otp", "input[name*='otp']", "input[id*='otp']", "input[name*='code']", "input[id*='code']"]:
                try:
                    if verbose:
                        print(f"→ Trying OTP selector: {sel}")
                    await page.fill(sel, code, timeout=5000)
                    filled = True
                    if verbose:
                        print(f"✓ OTP filled via {sel}")
                    break
                except Exception as e2:
                    if verbose:
                        print(f"→ Selector {sel} failed: {e2}")
                    continue
            if not filled:
                if attempt == 1:
                    if verbose:
                        print(f"✖ Unable to fill OTP code after all attempts")
                    raise AssertionError("Unable to fill OTP code")
                if verbose:
                    print(f"→ OTP fill failed, waiting 30s and retrying...")
                await page.wait_for_timeout(30000)
                continue

        # Submit OTP
        otp_submitted = False
        try:
            if verbose:
                print(f"→ Submitting OTP form")
            await page.get_by_role("button", name=re.compile(r"^(submit|continue|verify|sign\s*in)$", re.I)).first.click(timeout=6000)
            otp_submitted = True
            if verbose:
                print(f"✓ OTP submitted via role button")
        except Exception as e:
            if verbose:
                print(f"→ OTP submit by role failed: {e}, trying fallback selectors")
            for sel in ["button[type='submit']", "#submit"]:
                try:
                    if verbose:
                        print(f"→ Trying OTP submit selector: {sel}")
                    await page.click(sel, timeout=6000)
                    otp_submitted = True
                    if verbose:
                        print(f"✓ OTP submitted via {sel}")
                    break
                except Exception as e2:
                    if verbose:
                        print(f"→ Selector {sel} failed: {e2}")
                    continue
        if otp_submitted:
            if verbose:
                print(f"→ Waiting for page state after OTP submission...")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                await page.wait_for_timeout(2000)  # Fallback wait

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
                    if verbose:
                        print(f"→ Searching for Grant button in frame: {fr.url}")
                    loc = fr.get_by_role("button", name=grant_labels).first
                    if await loc.is_visible():
                        if verbose:
                            print(f"→ Found visible consent button in frame {fr.url}, clicking...")
                        await loc.click(timeout=6000)
                        if verbose:
                            print(f"→ Waiting for network idle after grant click...")
                        await fr.wait_for_load_state("networkidle")
                        if verbose:
                            print(f"✓ Consent grant successful in frame {fr.url}")
                        return True
                    else:
                        if verbose:
                            print(f"→ Grant button found but not visible in frame {fr.url}")
                except Exception as e:
                    if verbose:
                        print(f"→ Grant button search failed in frame {fr.url}: {e}")
                    pass
                try:
                    loc = fr.get_by_role("link", name=grant_labels).first
                    if await loc.is_visible():
                        if verbose:
                            print(f"→ Found visible consent link in frame {fr.url}, clicking...")
                        await loc.click(timeout=6000)
                        await fr.wait_for_load_state("networkidle")
                        if verbose:
                            print(f"✓ Consent grant link successful in frame {fr.url}")
                        return True
                except Exception as e:
                    if verbose:
                        print(f"→ Grant link search failed in frame {fr.url}: {e}")
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
        grant_clicked = False
        while page.context._impl_obj._loop.time() < end_grant:
            if await try_click_grant_anywhere():
                grant_clicked = True
                if verbose:
                    print(f"✓ Consent grant button clicked successfully")
                return
            await page.wait_for_timeout(250)
        if not grant_clicked and verbose:
            print(f"⚠️ Grant button not found/clicked, checking for redirect...")

        # Or success by redirect back to hub
        end = page.context._impl_obj._loop.time() + 10
        while page.context._impl_obj._loop.time() < end:
            if (urllib.parse.urlparse(page.url).hostname or "").endswith(base_host):
                return
            await page.wait_for_timeout(250)

        await page.wait_for_timeout(30000)

    raise AssertionError("OTP failed after 2 attempts")


async def run_test_suite(base_url: str, test_cases: list[dict], run_dir: Path, headless: bool = True, verbose: bool = False, model_id: str | None = None, region: str | None = None, repair: bool = False, agent_verify: bool = False, allow_direct_nav: bool = False) -> dict:
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

        # Sanitize deep-link navigations if not allowed
        def sanitize_tests(cases: list[dict]) -> list[dict]:
            if allow_direct_nav:
                return cases
            sanitized: list[dict] = []
            for t in cases:
                steps = []
                for s in t.get("steps", []):
                    act = s.get("action")
                    if act in ("navigate", "navigate_to"):
                        u = s.get("url") or s.get("target") or "/"
                        try:
                            parsed = urllib.parse.urlparse(u if u.startswith("http") else (base_url.rstrip("/") + "/" + u.lstrip("/")))
                            if parsed.path not in ("", "/"):
                                if verbose:
                                    print(f"↷ Removing deep-link navigate step (policy): {u}")
                                continue
                        except Exception:
                            pass
                    steps.append(s)
                sanitized.append({**t, "steps": steps})
            return sanitized

        test_cases = sanitize_tests(test_cases)

        # Log repair status at startup
        if verbose and repair:
            print(f"🔧 Repair mode ENABLED (model={model_id}, region={region})")
        elif verbose and not repair:
            print(f"⚠️  Repair mode DISABLED (use --repair to enable)")

        results = []
        session_logged_in = False
        
        # Helper function to generate descriptive screenshot names
        def sanitize_for_filename(text: str) -> str:
            """Sanitize text for use in filenames."""
            import re
            # Replace spaces and special chars with underscores
            text = re.sub(r'[^\w\s-]', '', text)
            text = re.sub(r'[-\s]+', '_', text)
            return text.strip('_').lower()[:100]  # Limit length
        
        def get_screenshot_path(test_name: str, step_index: int, action_type: str, context: str = "", extension: str = "png") -> Path:
            """Generate a descriptive screenshot path.
            
            Args:
                test_name: Name of the test case
                step_index: Step number (1-based)
                action_type: Type of screenshot (screenshot, verify, repair, failure, success)
                context: Additional context (e.g., "after-login", "manage-studies-page")
                extension: File extension (png or jpg)
            """
            test_slug = sanitize_for_filename(test_name)
            action_slug = sanitize_for_filename(action_type)
            context_slug = f"_{sanitize_for_filename(context)}" if context else ""
            filename = f"test_{test_slug}_step{step_index:02d}_{action_slug}{context_slug}.{extension}"
            return screenshots_dir / filename

        # Simple selector cache (persists across runs)
        cache_path = Path("data/selector_cache.json")
        try:
            if cache_path.exists():
                selector_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            else:
                selector_cache = {}
        except Exception:
            selector_cache = {}

        def cache_get(key: str):
            return selector_cache.get(key)

        def cache_put(key: str, value: dict):
            selector_cache[key] = value
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(selector_cache, indent=2), encoding="utf-8")
            except Exception:
                pass

        # Project overrides (user-editable)
        overrides_path = Path("data/selectors_overrides.json")
        try:
            if overrides_path.exists():
                user_overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
            else:
                user_overrides = {}
        except Exception:
            user_overrides = {}

        def get_override_candidates(slug: str) -> list[dict]:
            if not slug:
                return []
            cands = []
            if slug in DEFAULT_OVERRIDES:
                cands.extend(DEFAULT_OVERRIDES[slug])
            if slug in user_overrides:
                # user overrides win by being appended later
                cands.extend(user_overrides[slug])
            return cands

        async def open_user_menu_if_needed():
            # Try common triggers for a user/account menu
            patterns = [r"user", r"account", r"profile", r"menu", r"my\s*account", r"settings"]
            for pat in patterns:
                try:
                    loc = page.get_by_role("button", name=re.compile(pat, re.I)).first
                    if await loc.is_visible():
                        await loc.click(timeout=4000)
                        await page.wait_for_load_state("networkidle")
                        return True
                except Exception:
                    continue
            # Test ID variants
            for sel in [
                "[data-testid*='user']",
                "[data-testid*='account']",
                "#userMenu",
                ".user-menu",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click(timeout=4000)
                        await page.wait_for_load_state("networkidle")
                        return True
                except Exception:
                    continue
            return False

        async def is_logged_in() -> bool:
            if session_logged_in:
                return True
            # If we haven't navigated yet, we are not logged in
            try:
                cur = page.url
                if not cur or cur.startswith("about:"):
                    return False
            except Exception:
                return False
            # If a Login control is visible, we are not logged in
            try:
                loc = page.get_by_role("button", name=re.compile(r"^(login|log\s*in|sign\s*in)$", re.I)).first
                if await loc.is_visible():
                    return False
            except Exception:
                pass
            try:
                el = await page.query_selector("[data-testid='login-button']")
                if el and await el.is_visible():
                    return False
            except Exception:
                pass
            # Positive signals for logged-in
            try:
                el = await page.query_selector("[data-testid='user-menu']")
                if el and await el.is_visible():
                    return True
            except Exception:
                pass
            for pat in (r"user", r"account", r"profile"):
                try:
                    loc = page.get_by_role("button", name=re.compile(pat, re.I)).first
                    if await loc.is_visible():
                        return True
                except Exception:
                    continue
            # Default to True to avoid re-login loops when login controls are absent
            return True

        async def find_locator_any_frame(target: dict):
            """Return (frame, locator) for first visible match, else (None, None).
            target keys accepted:
              - engine: 'testid'|'css'|'text'|'role'
              - value/text/role/name_regex
            """
            frames = list({page.main_frame, *page.frames})
            for fr in frames:
                try:
                    engine = target.get("engine")
                    if engine == "testid":
                        loc = fr.get_by_test_id(target["value"]).first
                    elif engine == "css":
                        loc = fr.locator(target["value"])  # css selector
                    elif engine == "text":
                        loc = fr.get_by_text(target["text"], exact=False).first
                    elif engine == "role":
                        name_pattern = re.compile(target["name_regex"], re.I)
                        loc = fr.get_by_role(target["role"], name=name_pattern).first
                    else:
                        continue
                    try:
                        if await loc.is_visible():
                            return fr, loc
                    except Exception:
                        pass
                    # If not visible, still return if it exists; caller can try clicking/opening
                    try:
                        cnt = await loc.count()
                        if cnt and cnt > 0:
                            return fr, loc
                    except Exception:
                        pass
                except Exception:
                    continue
            return None, None

        def slug_to_text(slug: str) -> str:
            s = re.sub(r"[-_]+", " ", slug)
            s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
            return s.strip()

        async def resolve_target(selector: str, hints: dict | None = None):
            """Return (frame, locator, used_key) using cache and multi-strategy resolution."""
            # Build a stable cache key for strings or dicts
            if isinstance(selector, dict):
                cache_key = json.dumps({"target": selector, "hints": hints or {}}, sort_keys=True)
            else:
                cache_key = selector
            if hints:
                # include role/text hints in key to separate entries
                cache_key = json.dumps({"selector": selector if isinstance(selector, str) else selector, "hints": hints}, sort_keys=True)

            # Normalizations
            if isinstance(selector, str) and selector and "[text=" in selector:
                # Convert [text='...'] into text=...
                m = re.search(r"\[text=['\"](.+?)['\"]\]", selector)
                if m:
                    selector = f"text={m.group(1)}"

            # Normalize dict with {type,value} - but preserve original for verification
            original_selector_for_verification = selector.copy() if isinstance(selector, dict) else None
            if isinstance(selector, dict) and "type" in selector and "value" in selector and "text" not in selector and "css" not in selector and "data-testid" not in selector:
                t = (selector.get("type") or "").lower()
                v = selector.get("value")
                if t == "text":
                    selector = {"text": v, "_original_type": "text", "_original_value": v}  # Preserve for verification
                elif t in ("css",):
                    selector = {"css": v}
                elif t in ("testid", "data-testid"): 
                    selector = {"data-testid": v}

            cached = cache_get(cache_key)
            if cached:
                fr, loc = await find_locator_any_frame(cached)
                if fr:
                    return fr, loc, cache_key

            # Structured dict target
            if isinstance(selector, dict):
                if "data-testid" in selector:
                    fr, loc = await find_locator_any_frame({"engine": "testid", "value": selector["data-testid"]})
                    if fr:
                        cache_put(cache_key, {"engine": "testid", "value": selector["data-testid"]})
                        return fr, loc, cache_key
                # Handle role with name, value, or text
                if "role" in selector:
                    name_val = None
                    if "name" in selector:
                        name_val = selector["name"]
                    elif "value" in selector:
                        # Normalize "value" to "name" for role-based selectors
                        name_val = selector["value"]
                    elif "text" in selector:
                        name_val = selector["text"]
                    
                    if name_val:
                        if verbose:
                            print(f"🔍 resolve_target: Trying role='{selector['role']}', name='{name_val}'")
                        fr, loc = await find_locator_any_frame({"engine": "role", "role": selector["role"], "name_regex": re.escape(name_val)})
                        if fr:
                            if verbose:
                                print(f"🔍 resolve_target: Found frame for role='{selector['role']}', name='{name_val}'")
                            cache_put(cache_key, {"engine": "role", "role": selector["role"], "name_regex": re.escape(name_val)})
                            return fr, loc, cache_key
                        else:
                            if verbose:
                                print(f"🔍 resolve_target: No frame found for role='{selector['role']}', name='{name_val}'")
                if "text" in selector:
                    fr, loc = await find_locator_any_frame({"engine": "text", "text": selector["text"]})
                    if fr:
                        cache_put(cache_key, {"engine": "text", "text": selector["text"]})
                        return fr, loc, cache_key
                if "css" in selector:
                    fr, loc = await find_locator_any_frame({"engine": "css", "value": selector["css"]})
                    if fr:
                        cache_put(cache_key, {"engine": "css", "value": selector["css"]})
                        return fr, loc, cache_key
                # Handle "value" as standalone text selector if no other keys match
                if "value" in selector and "role" not in selector and "type" not in selector:
                    fr, loc = await find_locator_any_frame({"engine": "text", "text": selector["value"]})
                    if fr:
                        cache_put(cache_key, {"engine": "text", "text": selector["value"]})
                        return fr, loc, cache_key

            # 1) Native Playwright test id engine "data-testid=" style
            m = re.match(r"^data-testid\s*=\s*['\"]?([\w\-:]+)['\"]?$", selector) if isinstance(selector, str) else None
            if m:
                fr, loc = await find_locator_any_frame({"engine": "testid", "value": m.group(1)})
                if fr:
                    cache_put(cache_key, {"engine": "testid", "value": m.group(1)})
                    return fr, loc, cache_key

            # 2) CSS as-is (handles [data-testid='...'] etc.)
            if isinstance(selector, str) and any(c in selector for c in ("[", "]", ".", "#", ":", " ", ">")):
                fr, loc = await find_locator_any_frame({"engine": "css", "value": selector})
                if fr:
                    cache_put(cache_key, {"engine": "css", "value": selector})
                    return fr, loc, cache_key
                # If CSS with aria-label fails, try role+name from aria-label
                m_al = re.search(r"aria-label=['\"](.+?)['\"]", selector)
                if m_al:
                    name = m_al.group(1)
                    for role in ("button", "link"):
                        fr, loc = await find_locator_any_frame({"engine": "role", "role": role, "name_regex": re.escape(name)})
                        if fr:
                            cache_put(cache_key, {"engine": "role", "role": role, "name_regex": re.escape(name)})
                            return fr, loc, cache_key

            # 3) text= engine
            if isinstance(selector, str) and selector.startswith("text="):
                text = selector.split("=", 1)[1]
                fr, loc = await find_locator_any_frame({"engine": "text", "text": text})
                if fr:
                    cache_put(cache_key, {"engine": "text", "text": text})
                    return fr, loc, cache_key

            # 3b) role= engine (e.g., role=table)
            m_role = re.match(r"^role\s*=\s*([\w-]+)$", selector) if isinstance(selector, str) else None
            if m_role:
                role = m_role.group(1)
                fr, loc = await find_locator_any_frame({"engine": "role", "role": role, "name_regex": ".*"})
                if fr:
                    cache_put(cache_key, {"engine": "role", "role": role, "name_regex": ".*"})
                    return fr, loc, cache_key

            # 4) Try alternate attribute candidates for a slug
            slug = selector.strip().strip("'").strip('"') if isinstance(selector, str) else ""
            candidates_css = [
                f"[data-testid='{slug}']",
                f"[data-test-id='{slug}']",
                f"[data-qa='{slug}']",
                f"#{slug}",
                f"[name='{slug}']",
            ] if slug else []
            # Overrides for well-known slugs
            for t in get_override_candidates(slug):
                fr, loc = await find_locator_any_frame(t)
                if fr:
                    cache_put(cache_key, t)
                    return fr, loc, cache_key
            for css in candidates_css:
                fr, loc = await find_locator_any_frame({"engine": "css", "value": css})
                if fr:
                    cache_put(cache_key, {"engine": "css", "value": css})
                    return fr, loc, cache_key

            # 5) Role + humanized name
            human = slug_to_text(slug) if slug else ""
            for role in ("menuitem", "link", "button"):
                fr, loc = await find_locator_any_frame({"engine": "role", "role": role, "name_regex": re.escape(human)})
                if fr:
                    cache_put(cache_key, {"engine": "role", "role": role, "name_regex": re.escape(human)})
                    return fr, loc, cache_key

            # 6) Text contains humanized name
            if human:
                fr, loc = await find_locator_any_frame({"engine": "text", "text": human})
                if fr:
                    cache_put(cache_key, {"engine": "text", "text": human})
                    return fr, loc, cache_key

            # 7) As absolute fallback, try opening user menu once then retry CSS candidates
            await open_user_menu_if_needed()
            for css in candidates_css:
                fr, loc = await find_locator_any_frame({"engine": "css", "value": css})
                if fr:
                    cache_put(cache_key, {"engine": "css", "value": css})
                    return fr, loc, cache_key

            return None, None, cache_key

        def selector_to_repair_string(selector) -> str:
            """Convert a selector (dict or string) to a meaningful string for repair."""
            if isinstance(selector, dict):
                # Convert dict to a readable format
                parts = []
                if "data-testid" in selector:
                    parts.append(f"data-testid='{selector['data-testid']}'")
                if "type" in selector and "value" in selector:
                    if selector["type"] == "data-testid":
                        parts.append(f"data-testid='{selector['value']}'")
                    elif selector["type"] == "text":
                        parts.append(f"text='{selector['value']}'")
                    else:
                        parts.append(f"{selector['type']}='{selector['value']}'")
                if "role" in selector:
                    role_part = f"role='{selector['role']}'"
                    if "name" in selector:
                        role_part += f" name='{selector['name']}'"
                    elif "value" in selector:
                        # Normalize "value" to "name" for role-based selectors
                        role_part += f" name='{selector['value']}'"
                    elif "text" in selector:
                        role_part += f" text='{selector['text']}'"
                    parts.append(role_part)
                if "text" in selector and "role" not in selector:
                    parts.append(f"text='{selector['text']}'")
                if "css" in selector:
                    parts.append(f"css='{selector['css']}'")
                # Handle standalone "value" key (not part of type/role)
                if "value" in selector and "type" not in selector and "role" not in selector:
                    parts.append(f"text='{selector['value']}'")
                return " ".join(parts) if parts else json.dumps(selector)
            return str(selector)

        async def agent_repair(selector, context_hint: str = "", test_name: str = "unknown", step_index: int = 0) -> list[str]:
            """Ask the agent for alternative selectors. Returns a list of suggested selectors."""
            if not repair or not model_id or not region:
                return []
            try:
                # Convert selector to string for repair prompt
                selector_str = selector_to_repair_string(selector)
                
                # Get element inventory for context
                try:
                    inventory = await build_element_inventory(page, limit=100)
                except Exception:
                    inventory = {}
                
                # Get DOM snapshot for better context
                dom_snapshot = ""
                try:
                    # Get a simplified DOM structure (focusing on attributes useful for selectors)
                    dom_snapshot = await page.evaluate("""
                        () => {
                            function extractSelectorInfo(el, depth = 0) {
                                if (depth > 5) return null; // Limit depth
                                const info = {
                                    tag: el.tagName?.toLowerCase(),
                                    id: el.id || null,
                                    testid: el.getAttribute('data-testid') || null,
                                    class: el.className?.baseVal || el.className || null,
                                    role: el.getAttribute('role') || null,
                                    'aria-label': el.getAttribute('aria-label') || null,
                                    text: (el.innerText || el.textContent || '').trim().substring(0, 50) || null,
                                    children: []
                                };
                                
                                // Limit children to first 10
                                Array.from(el.children || []).slice(0, 10).forEach(child => {
                                    const childInfo = extractSelectorInfo(child, depth + 1);
                                    if (childInfo) info.children.push(childInfo);
                                });
                                
                                return info;
                            }
                            
                            return JSON.stringify(extractSelectorInfo(document.body), null, 2);
                        }
                    """)
                except Exception as e:
                    if verbose:
                        print(f"⚠️ Could not extract DOM snapshot: {e}")
                    dom_snapshot = "DOM extraction failed"
                
                # Capture screenshot for visual context
                import base64
                shot_bytes = None
                try:
                    shot_bytes = await page.screenshot(full_page=True, type="jpeg", quality=70)
                    img_payload = [{"media_type": "image/jpeg", "data_base64": base64.b64encode(shot_bytes).decode("ascii")}]
                    # Save repair screenshot for debugging (if we have context)
                    # Note: This will be saved later when we have test/step context
                except Exception as e:
                    if verbose:
                        print(f"⚠️ Could not capture screenshot: {e}")
                    img_payload = []
                
                # Enhanced prompt with visual and DOM context
                from story_agent import bedrock_invoke_claude, bedrock_invoke_claude_multimodal
                
                prompt = (
                    "You are a test selector repair assistant. Given a failed selector, a page screenshot, DOM structure, and available elements, propose up to 3 alternative selectors.\n"
                    "Rules: Only output a JSON array of strings; each must be a CSS selector, data-testid form, text selector, or role-based selector. No prose.\n"
                    "Prefer stable selectors: data-testid > role+name > id > aria-label > class. Avoid fragile CSS selectors.\n\n"
                    f"Failed selector: {selector_str}\n"
                    f"Current URL: {context_hint}\n"
                    f"Available testids: {json.dumps(inventory.get('testids', []), indent=2)}\n"
                    f"Available buttons (by text): {json.dumps(inventory.get('buttons', []), indent=2)}\n"
                    f"Available menuitems (by text): {json.dumps(inventory.get('menuitems', []), indent=2)}\n"
                    f"DOM structure (simplified, first 2000 chars):\n{dom_snapshot[:2000]}\n"
                )
                
                if verbose:
                    print(f"🔧 Repairing selector: {selector_str}")
                    if img_payload:
                        print(f"🔧 Using screenshot + DOM for context")
                    else:
                        print(f"🔧 Using DOM only (screenshot unavailable)")
                
                # Use multimodal if screenshot available, otherwise text-only
                if img_payload:
                    raw = bedrock_invoke_claude_multimodal(prompt, images=img_payload, model_id=model_id, region=region, verbose=verbose)
                    # Save repair screenshot for debugging
                    if shot_bytes:
                        try:
                            selector_slug = sanitize_for_filename(selector_str[:50])
                            repair_path = get_screenshot_path(test_name, step_index, "repair", context=selector_slug, extension="jpg")
                            repair_path.write_bytes(shot_bytes)
                            if verbose:
                                print(f"📸 Repair screenshot saved: {repair_path.name}")
                        except Exception as e:
                            if verbose:
                                print(f"⚠️ Could not save repair screenshot: {e}")
                else:
                    raw = bedrock_invoke_claude(prompt, model_id=model_id, region=region, verbose=verbose)
                try:
                    # Clean up JSON extraction
                    body = raw.strip()
                    if "```json" in body:
                        body = body.split("```json")[1].split("```")[0].strip()
                    elif "```" in body:
                        body = body.split("```")[1].split("```")[0].strip()
                    arr = json.loads(body)
                    if isinstance(arr, list):
                        if verbose:
                            print(f"🔧 Repair suggestions: {arr}")
                        return [s for s in arr if isinstance(s, str) and s]
                except Exception as e:
                    if verbose:
                        print(f"🔧 Repair returned non-JSON or unusable content: {e}")
                    return []
            except Exception as e:
                if verbose:
                    print(f"🔧 Repair error: {e}")
                return []

        async def resolve_with_repair(selector, hints: dict | None = None, verify_exists: bool = False, test_name: str = "unknown", step_index: int = 0):
            """Resolve selector with repair support.
            
            Args:
                selector: The selector to resolve
                hints: Optional hints for resolution
                verify_exists: If True, verify element actually exists (count > 0) before considering it resolved
            """
            if verbose:
                print(f"🔍 resolve_with_repair called: selector={selector}, verify_exists={verify_exists}, repair={repair}")
            
            fr, loc, key = await resolve_target(selector, hints)
            if verbose:
                print(f"🔍 resolve_target returned: fr={fr is not None}, key={key}")
            
            if fr:
                # If verify_exists is True, check that the element actually exists AND matches
                if verify_exists:
                    try:
                        cnt = await loc.count()
                        if verbose:
                            print(f"🔍 Element count check: cnt={cnt}")
                        if not cnt or cnt <= 0:
                            if verbose:
                                print(f"🔧 Selector resolved but element count is 0, treating as not found: {selector}")
                            fr = None  # Treat as not found
                        else:
                            # Verify the element actually matches what we're looking for
                            if isinstance(selector, dict):
                                # For role-based selectors, verify the accessible name matches
                                if "role" in selector:
                                    expected_name = selector.get("name") or selector.get("value") or selector.get("text")
                                    if expected_name:
                                        try:
                                            # Use Playwright's accessible name (most reliable)
                                            actual_name = await loc.evaluate("""
                                                el => {
                                                    // Try aria-label first
                                                    if (el.getAttribute('aria-label')) {
                                                        return el.getAttribute('aria-label').trim();
                                                    }
                                                    // Then try innerText
                                                    if (el.innerText) {
                                                        return el.innerText.trim();
                                                    }
                                                    // Then textContent
                                                    if (el.textContent) {
                                                        return el.textContent.trim();
                                                    }
                                                    return '';
                                                }
                                            """)
                                            actual_name = actual_name or ""
                                            
                                            # Check if names match (case-insensitive, allow partial/substring match)
                                            # But reject empty actual_name unless expected is also empty
                                            expected_lower = expected_name.lower().strip()
                                            actual_lower = actual_name.lower().strip()
                                            
                                            # Reject if actual name is empty but we expected something
                                            if not actual_lower and expected_lower:
                                                if verbose:
                                                    print(f"🔧 Element found but name is empty (expected '{expected_name}'), treating as not found")
                                                fr = None  # Treat as not found - will trigger repair
                                            elif expected_lower not in actual_lower and actual_lower not in expected_lower:
                                                if verbose:
                                                    print(f"🔧 Element found but name mismatch: expected '{expected_name}', got '{actual_name}', treating as not found")
                                                fr = None  # Treat as not found - will trigger repair
                                            else:
                                                if verbose:
                                                    print(f"✓ Element found with matching name: '{actual_name}' (expected '{expected_name}')")
                                        except Exception as e:
                                            if verbose:
                                                print(f"⚠️ Could not verify element name, assuming match: {e}")
                                # For text-based selectors, verify the text content matches
                                # Check both original form and normalized form
                                elif selector.get("type") == "text" or "text" in selector or selector.get("_original_type") == "text":
                                    expected_text = selector.get("value") or selector.get("text") or selector.get("_original_value")
                                    if expected_text:
                                        try:
                                            # Get text content of the element
                                            actual_text = await loc.evaluate("""
                                                el => {
                                                    // Try innerText first (more accurate)
                                                    if (el.innerText) {
                                                        return el.innerText.trim();
                                                    }
                                                    // Then textContent
                                                    if (el.textContent) {
                                                        return el.textContent.trim();
                                                    }
                                                    // Then aria-label
                                                    if (el.getAttribute('aria-label')) {
                                                        return el.getAttribute('aria-label').trim();
                                                    }
                                                    return '';
                                                }
                                            """)
                                            actual_text = actual_text or ""
                                            
                                            expected_lower = expected_text.lower().strip()
                                            actual_lower = actual_text.lower().strip()
                                            
                                            # Reject if actual text is empty but we expected something
                                            if not actual_lower and expected_lower:
                                                if verbose:
                                                    print(f"🔧 Element found but text is empty (expected '{expected_text}'), treating as not found")
                                                fr = None  # Treat as not found - will trigger repair
                                            elif expected_lower not in actual_lower and actual_lower not in expected_lower:
                                                if verbose:
                                                    print(f"🔧 Element found but text mismatch: expected '{expected_text}', got '{actual_text}', treating as not found")
                                                fr = None  # Treat as not found - will trigger repair
                                            else:
                                                if verbose:
                                                    print(f"✓ Element found with matching text: '{actual_text}' (expected '{expected_text}')")
                                        except Exception as e:
                                            if verbose:
                                                print(f"⚠️ Could not verify element text, assuming match: {e}")
                                else:
                                    if verbose:
                                        print(f"✓ Element found with count {cnt} (no text/name verification needed)")
                            else:
                                if verbose:
                                    print(f"✓ Element found with count {cnt}")
                    except Exception as e:
                        if verbose:
                            print(f"🔧 Error checking element count: {e}, treating as not found")
                        fr = None
                else:
                    if verbose:
                        print(f"⚠️ verify_exists=False, skipping count check")
                if fr:
                    if verbose:
                        print(f"✓ Returning resolved element (fr is set)")
                    return fr, loc, key
                else:
                    if verbose:
                        print(f"✖ fr was cleared by verify_exists check, will attempt repair")
            else:
                if verbose:
                    print(f"✖ resolve_target returned None, will attempt repair")
            
            # Agent repair attempt once - only if repair is enabled
            if repair and model_id and region:
                if verbose:
                    print(f"🔧 Selector not found, attempting repair: {selector}")
                suggestions = await agent_repair(selector, context_hint=page.url, test_name=test_name, step_index=step_index)
                if not suggestions:
                    if verbose:
                        print(f"🔧 No repair suggestions returned from agent")
                else:
                    if verbose:
                        print(f"🔧 Got {len(suggestions)} repair suggestions")
                for i, sug in enumerate(suggestions[:3], 1):
                    if verbose:
                        print(f"🔧 Trying repair suggestion {i}/{len(suggestions[:3])}: {sug}")
                    fr2, loc2, key2 = await resolve_target(sug, hints)
                    if fr2:
                        # Verify the repaired selector actually works
                        try:
                            cnt = await loc2.count()
                            if cnt and cnt > 0:
                                if verbose:
                                    print(f"✓ Repair successful with: {sug} (count={cnt})")
                                return fr2, loc2, key2
                            else:
                                if verbose:
                                    print(f"✖ Repair suggestion returned element with count 0: {sug}")
                        except Exception as e:
                            if verbose:
                                print(f"✖ Repair suggestion error: {e}")
                    else:
                        if verbose:
                            print(f"✖ Repair suggestion could not resolve: {sug}")
                if verbose:
                    print(f"✖ Repair did not find working alternative")
            else:
                if verbose:
                    repair_status = f"repair={repair}, model_id={model_id}, region={region}"
                    print(f"⚠️ Repair not enabled: {repair_status}")
            if verbose:
                print(f"✖ Returning None from resolve_with_repair")
            return None, None, key

        async def agent_verify_state_if_enabled(step: dict, test_name: str, step_index: int) -> str:
            if not agent_verify or not model_id or not region:
                return ""
            # Lightweight DOM snapshot
            try:
                inventory = await build_element_inventory(page, limit=100)
            except Exception:
                inventory = {"error": "inventory_failed"}
            from story_agent import bedrock_invoke_claude_multimodal
            if verbose:
                print(f"→ Verifying step via agent (url={page.url})")
                try:
                    print(f"→ Verify step summary: action={step.get('action')} target={step.get('target') or step.get('selector')}")
                except Exception:
                    pass
            prompt = (
                "You are verifying a UI test step result. Output only JSON: {\"ok\": boolean, \"reason\": string}.\n"
                "Given the current URL, the expected step, a page screenshot, and a summary of present elements, decide if the step truly succeeded.\n"
                f"Current URL: {page.url}\n"
                f"Step: {json.dumps(step)}\n"
                f"Elements: {json.dumps(inventory)}\n"
            )
            # Capture an on-the-fly JPEG screenshot for verification
            import base64
            shot_bytes = await page.screenshot(full_page=True, type="jpeg", quality=70)
            img_payload = [{"media_type": "image/jpeg", "data_base64": base64.b64encode(shot_bytes).decode("ascii")}]
            try:
                raw = bedrock_invoke_claude_multimodal(prompt, images=img_payload, model_id=model_id, region=region, verbose=verbose)
            except Exception as ve:
                if verbose:
                    print(f"🤖 Verify error/non-blocking: transport error: {ve}")
                return ""
            verdict = {}
            try:
                body = raw.strip()
                # Strip code fences if present
                if body.startswith("```"):
                    body = body.replace("```json", "").replace("```", "").strip()
                # Fallback: extract between first { and last }
                if not body.strip().startswith("{"):
                    start = body.find("{")
                    end = body.rfind("}")
                    if start != -1 and end != -1 and end > start:
                        body = body[start:end+1]
                verdict = json.loads(body)
            except Exception:
                verdict = {"ok": True, "reason": "Agent returned non-JSON; skipping enforcement"}
            if verbose:
                print(f"🤖 Verify verdict: {verdict}")
            # Always persist verification screenshot with descriptive name
            try:
                verify_path = get_screenshot_path(test_name, step_index, "verify", context="agent_check", extension="jpg")
                verify_path.write_bytes(shot_bytes)
                saved_path = str(verify_path)
                if verbose:
                    print(f"📸 Verification screenshot saved: {verify_path.name}")
            except Exception as e:
                if verbose:
                    print(f"⚠️ Could not save verification screenshot: {e}")
                saved_path = ""
            if isinstance(verdict, dict) and verdict.get("ok") is False:
                # Fail the step based on agent's truth
                raise AssertionError(f"Agent verification failed: {verdict.get('reason','no reason')}")
            return saved_path

        for test in test_cases:
            if verbose:
                print(f"\n===== Running Test: {test.get('name','Unnamed')} =====")
            status = "passed"
            error = ""
            screenshot = ""
            try:
                for idx, step in enumerate(test.get("steps", []), start=1):
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
                        # Always ensure base page and consent before checking login
                        await page.goto(base_url, timeout=60000)
                        await page.wait_for_load_state("networkidle")
                        await consent_dismiss(page, verbose=verbose)
                        if session_logged_in:
                            if verbose:
                                print("→ Session says logged in; skipping login_via_login_gov")
                        else:
                            # Decide based on Login button visibility only
                            should_login = await login_button_visible(page)
                            if verbose:
                                print(f"→ Login button visible: {should_login}")
                            if not should_login:
                                session_logged_in = True
                                if verbose:
                                    print("→ No Login button; treating as logged in")
                            else:
                                username_env = step.get("username_env", "LOGIN_USERNAME")
                                password_env = step.get("password_env", "LOGIN_PASSWORD")
                                totp_env = step.get("totp_env", "TOTP_SECRET")
                                
                                username = os.environ.get(username_env, "")
                                password = os.environ.get(password_env, "")
                                secret = os.environ.get(totp_env, "")
                                
                                # Debug: show which env vars are checked
                                if verbose:
                                    print(f"→ Checking env vars: {username_env}={bool(username)}, {password_env}={bool(password)}, {totp_env}={bool(secret)}")
                                
                                # Provide detailed error message about which variables are missing
                                missing = []
                                if not username:
                                    missing.append(username_env)
                                if not password:
                                    missing.append(password_env)
                                if not secret:
                                    missing.append(totp_env)
                                
                                if missing:
                                    error_msg = f"Missing required environment variables: {', '.join(missing)}. Please set these before running tests."
                                    if verbose:
                                        print(f"✖ {error_msg}")
                                    raise AssertionError(error_msg)
                                await click_login_button(page, verbose=verbose)
                                await click_login_gov(page, verbose=verbose)
                                await fill_credentials_and_submit(page, username, password, verbose=verbose)
                                await handle_otp_and_consent(page, secret, (urllib.parse.urlparse(base_url).hostname or ""), verbose=verbose)
                                session_logged_in = True
                                # Build and store element inventory for later repair/agent context
                                try:
                                    inventory = await build_element_inventory(page)
                                    inv_path = Path("data/runs") / f"run_{os.environ.get('RUN_ID','latest')}" / "element_inventory.json"
                                    inv_path.parent.mkdir(parents=True, exist_ok=True)
                                    inv_path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
                                    if verbose:
                                        print(f"🧭 Element inventory saved to {inv_path}")
                                except Exception as e:
                                    if verbose:
                                        print(f"🧭 Element inventory failed: {e}")
                    elif action in ("assert_text", "assert_text_present", "assert_visible"):
                        text = step.get("text")
                        sel = step.get("selector") or step.get("target")
                        loc = None
                        if isinstance(sel, dict) and sel.get("text"):
                            loc = page.get_by_text(sel.get("text"), exact=False).first
                        elif isinstance(sel, dict) and sel.get("type") == "text":
                            loc = page.get_by_text(sel.get("value"), exact=False).first
                        elif isinstance(sel, str) and sel.startswith("text="):
                            loc = page.get_by_text(sel.split("=",1)[1], exact=False).first
                        else:
                            loc = page.get_by_text(text or str(sel), exact=False).first
                        await loc.wait_for(state="visible", timeout=8000)
                        _ver_path = await agent_verify_state_if_enabled(step, test.get('name','Unnamed'), idx)
                    elif action in ("assert_element_present", "assert_element_presence", "assert_element_exists", "assert_element_visible", "assert_element", "assert"):
                        selector = step.get("selector") or step.get("target")
                        if verbose:
                            print(f"🔍 Assert action: resolving selector {selector}")
                        # For assert actions, verify element exists (count > 0) during resolution
                        fr, loc, key = await resolve_with_repair(selector, hints=None, verify_exists=True, test_name=test.get("name", "unknown"), step_index=idx)
                        if not fr:
                            if verbose:
                                print(f"🔍 First attempt failed, trying to open user menu and retry")
                            # Try opening user menu then retry
                            await open_user_menu_if_needed()
                            fr, loc, key = await resolve_with_repair(selector, hints=None, verify_exists=True, test_name=test.get("name", "unknown"), step_index=idx)
                        if not fr:
                            error_msg = f"Could not resolve selector: {selector}"
                            if repair and model_id and region:
                                error_msg += " (repair was attempted but found no working alternatives)"
                            if verbose:
                                print(f"✖ {error_msg}")
                            raise AssertionError(error_msg)
                        if verbose:
                            print(f"✓ Assert selector resolved successfully")
                        # Presence vs visibility semantics
                        expect_exists = step.get("exists") if "exists" in step else step.get("existence") if "existence" in step else None
                        if expect_exists is False:
                            # Ensure not visible/present
                            visible = False
                            try:
                                visible = await loc.is_visible()
                            except Exception:
                                visible = False
                            if visible:
                                raise AssertionError(f"Element should not be visible: {selector}")
                        elif expect_exists is True or action in ("assert_element_present", "assert_element_presence", "assert_element_exists", "assert_element"):
                            # Only require presence (not visibility)
                            cnt = await loc.count()
                            if not cnt or cnt <= 0:
                                # Element was resolved but count is 0 - try repair one more time
                                if repair and model_id and region and verbose:
                                    print(f"🔧 Element resolved but count is 0, attempting repair: {selector}")
                                if repair and model_id and region:
                                    await open_user_menu_if_needed()
                                    fr2, loc2, key2 = await resolve_with_repair(selector, hints=None, test_name=test.get("name", "unknown"), step_index=idx)
                                    if fr2:
                                        cnt2 = await loc2.count()
                                        if cnt2 and cnt2 > 0:
                                            loc = loc2
                                            fr = fr2
                                            key = key2
                                            cnt = cnt2
                                if not cnt or cnt <= 0:
                                    error_msg = f"Element not found: {selector}"
                                    if repair and model_id and region:
                                        error_msg += " (repair was attempted but found no working alternatives)"
                                    raise AssertionError(error_msg)
                        else:
                            # Default behavior: require visibility
                            try:
                                await loc.wait_for(state="visible", timeout=8000)
                            except Exception:
                                # Trigger repair on visibility timeout as well
                                fr, loc, key = await resolve_with_repair(selector, hints=None, verify_exists=True, test_name=test.get("name", "unknown"), step_index=idx)
                                if not fr:
                                    raise AssertionError(f"Element not visible after repair attempt: {selector}")
                                await loc.wait_for(state="visible", timeout=5000)
                        _ver_path = await agent_verify_state_if_enabled(step, test.get('name','Unnamed'), idx)
                    elif action == "click":
                        selector = step.get("selector") or step.get("target")
                        # For click actions, verify element exists (count > 0) during resolution
                        fr, loc, key = await resolve_with_repair(selector, hints=None, verify_exists=True, test_name=test.get("name", "unknown"), step_index=idx)
                        if not fr:
                            await open_user_menu_if_needed()
                            fr, loc, key = await resolve_with_repair(selector, hints=None, verify_exists=True, test_name=test.get("name", "unknown"), step_index=idx)
                        if not fr:
                            error_msg = f"Could not resolve selector for click: {selector}"
                            if repair and model_id and region:
                                error_msg += " (repair was attempted but found no working alternatives)"
                            raise AssertionError(error_msg)
                        # If it is a link, ensure allowlisted
                        try:
                            href = await loc.get_attribute("href")
                            if href and not host_allowed(href, urllib.parse.urlparse(page.url).hostname or ""):
                                raise AssertionError(f"Blocked click to external link: {href}")
                        except Exception:
                            pass
                        await loc.click(timeout=10000)
                        await page.wait_for_load_state("networkidle")
                    elif action in ("navigate_to", "navigate"):
                        url = step.get("url") or step.get("target") or "/"
                        target = url if url.startswith("http") else base_url.rstrip("/") + "/" + url.lstrip("/")
                        # Guard direct navigation to deep paths unless explicitly allowed
                        if not allow_direct_nav:
                            # Permit base URL or "/" only; block if path beyond root and the step did not originate from a click
                            try:
                                parsed = urllib.parse.urlparse(target)
                                if parsed.path not in ("", "/"):
                                    raise AssertionError(f"Direct navigation blocked by policy: {target}. Use clicks to reach this page or run with --allow-direct-nav.")
                            except Exception as nav_err:
                                raise AssertionError(str(nav_err))
                        await page.goto(target, timeout=60000)
                        await page.wait_for_load_state("networkidle")
                        await consent_dismiss(page, verbose=verbose)
                    elif action in ("assert_url_matches", "assert_url_contains"):
                        expected = step.get("value") or step.get("target") or ""
                        cur = page.url
                        if expected and expected not in cur:
                            raise AssertionError(f"URL '{cur}' does not contain '{expected}'")
                    elif action in ("screenshot",):
                        # Use custom name from step if provided, otherwise generate descriptive name
                        custom_name = step.get("name", "")
                        if custom_name:
                            context = sanitize_for_filename(custom_name)
                        else:
                            # Generate context from step action/target
                            context_parts = []
                            if step.get("action"):
                                context_parts.append(step.get("action"))
                            if step.get("target") or step.get("selector"):
                                target = step.get("target") or step.get("selector")
                                if isinstance(target, dict):
                                    if target.get("data-testid"):
                                        context_parts.append(target.get("data-testid"))
                                    elif target.get("text") or target.get("value"):
                                        context_parts.append(target.get("text") or target.get("value"))
                                    elif target.get("role"):
                                        context_parts.append(target.get("role"))
                            context = "_".join([sanitize_for_filename(str(p)) for p in context_parts if p]) or "screenshot"
                        shot = get_screenshot_path(test.get("name", "unknown"), idx, "screenshot", context=context, extension="png")
                        # Optional delay before screenshot to allow UI to settle
                        try:
                            delay_ms = int(os.environ.get("SCREENSHOT_DELAY_MS", "2000"))
                        except Exception:
                            delay_ms = 2000
                        if delay_ms > 0:
                            await page.wait_for_timeout(delay_ms)
                        await page.screenshot(path=str(shot), full_page=True)
                        screenshot = str(shot)
                        if verbose:
                            print(f"📸 Screenshot saved: {shot.name}")
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
                    # Generate descriptive failure screenshot name with step info
                    error_context = sanitize_for_filename(error.split("(")[0].strip()[:50]) if error else "error"
                    shot = get_screenshot_path(test.get("name", "unknown"), idx, "failure", context=error_context, extension="png")
                    # Apply same pre-screenshot delay on failure captures
                    try:
                        delay_ms = int(os.environ.get("SCREENSHOT_DELAY_MS", "2000"))
                    except Exception:
                        delay_ms = 2000
                    if delay_ms > 0:
                        await page.wait_for_timeout(delay_ms)
                    await page.screenshot(path=str(shot), full_page=True)
                    screenshot = str(shot)
                    if verbose:
                        print(f"📸 Failure screenshot saved: {shot.name}")
                except Exception as e:
                    if verbose:
                        print(f"⚠️ Could not save failure screenshot: {e}")
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


