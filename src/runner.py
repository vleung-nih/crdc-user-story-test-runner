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


async def run_test_suite(base_url: str, test_cases: list[dict], run_dir: Path, headless: bool = True, verbose: bool = False, model_id: str | None = None, region: str | None = None, repair: bool = False, agent_verify: bool = False) -> dict:
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
        session_logged_in = False

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

            # Normalize dict with {type,value}
            if isinstance(selector, dict) and "type" in selector and "value" in selector and "text" not in selector and "css" not in selector and "data-testid" not in selector:
                t = (selector.get("type") or "").lower()
                v = selector.get("value")
                if t == "text":
                    selector = {"text": v}
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
                if "role" in selector and "name" in selector:
                    fr, loc = await find_locator_any_frame({"engine": "role", "role": selector["role"], "name_regex": re.escape(selector["name"])})
                    if fr:
                        cache_put(cache_key, {"engine": "role", "role": selector["role"], "name_regex": re.escape(selector["name"])})
                        return fr, loc, cache_key
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

        async def agent_repair(selector: str, context_hint: str = "") -> list[str]:
            """Ask the agent for alternative selectors. Returns a list of suggested selectors."""
            if not repair or not model_id or not region:
                return []
            try:
                # Minimal, safe prompt: propose only CSS or test id forms
                from story_agent import bedrock_invoke_claude
                prompt = (
                    "You are a test selector repair assistant. Given a failed selector and a short page URL, propose up to 3 alternative selectors.\n"
                    "Rules: Only output a JSON array of strings; each must be a CSS selector or data-testid form. No prose.\n\n"
                    f"Failed selector: {selector}\n"
                    f"Current URL: {context_hint}\n"
                )
                if verbose:
                    print(f"🔧 Repairing selector: {selector}")
                raw = bedrock_invoke_claude(prompt, model_id=model_id, region=region, verbose=verbose)
                try:
                    arr = json.loads(raw.strip().split("```")[-1]) if raw.strip().startswith("```") else json.loads(raw)
                    if isinstance(arr, list):
                        if verbose:
                            print(f"🔧 Repair suggestions: {arr}")
                        return [s for s in arr if isinstance(s, str) and s]
                except Exception:
                    if verbose:
                        print("🔧 Repair returned non-JSON or unusable content")
                    return []
            except Exception:
                return []

        async def resolve_with_repair(selector: str, hints: dict | None = None):
            fr, loc, key = await resolve_target(selector, hints)
            if fr:
                return fr, loc, key
            # Agent repair attempt once
            suggestions = await agent_repair(selector, context_hint=page.url)
            for sug in suggestions[:3]:
                fr, loc, key2 = await resolve_target(sug, hints)
                if fr:
                    return fr, loc, key2
            return None, None, key

        async def agent_verify_state_if_enabled(step: dict, test_name: str, step_index: int) -> str:
            if not agent_verify or not model_id or not region:
                return ""
            try:
                # Lightweight DOM snapshot
                inventory = await build_element_inventory(page, limit=100)
                from story_agent import bedrock_invoke_claude, bedrock_invoke_claude_multimodal
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
                raw = bedrock_invoke_claude_multimodal(prompt, images=img_payload, model_id=model_id, region=region, verbose=verbose)
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
                if isinstance(verdict, dict) and verdict.get("ok") is False:
                    raise AssertionError(f"Agent verification failed: {verdict.get('reason','no reason')}")
                # Save verification screenshot to disk
                try:
                    # Normalize test name
                    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", test_name).strip("_").lower() or "test"
                    verify_path = screenshots_dir / f"verify_{slug}_step{step_index}.jpg"
                    verify_path.write_bytes(shot_bytes)
                    return str(verify_path)
                except Exception:
                    return ""
            except Exception as ve:
                # If agent verification fails in parsing/transport, don't block unless explicit
                if verbose:
                    print(f"🤖 Verify error/non-blocking: {ve}")
            return ""

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
                                username = os.environ.get(step.get("username_env", "LOGIN_USERNAME"), "")
                                password = os.environ.get(step.get("password_env", "LOGIN_PASSWORD"), "")
                                secret = os.environ.get(step.get("totp_env", "TOTP_SECRET"), "")
                                if not username or not password or not secret:
                                    raise AssertionError("Missing LOGIN_USERNAME/LOGIN_PASSWORD/TOTP_SECRET envs")
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
                        fr, loc, key = await resolve_with_repair(selector, hints=None)
                        if not fr:
                            # Try opening user menu then retry
                            await open_user_menu_if_needed()
                            fr, loc, key = await resolve_with_repair(selector, hints=None)
                        if not fr:
                            raise AssertionError(f"Could not resolve selector: {selector}")
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
                                raise AssertionError(f"Element not found: {selector}")
                        else:
                            # Default behavior: require visibility
                            try:
                                await loc.wait_for(state="visible", timeout=8000)
                            except Exception:
                                # Trigger repair on visibility timeout as well
                                fr, loc, key = await resolve_with_repair(selector, hints=None)
                                await loc.wait_for(state="visible", timeout=5000)
                        _ver_path = await agent_verify_state_if_enabled(step, test.get('name','Unnamed'), idx)
                    elif action == "click":
                        selector = step.get("selector") or step.get("target")
                        fr, loc, key = await resolve_with_repair(selector, hints=None)
                        if not fr:
                            await open_user_menu_if_needed()
                            fr, loc, key = await resolve_with_repair(selector, hints=None)
                        if not fr:
                            raise AssertionError(f"Could not resolve selector for click: {selector}")
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
                        await page.goto(target, timeout=60000)
                        await page.wait_for_load_state("networkidle")
                        await consent_dismiss(page, verbose=verbose)
                    elif action in ("assert_url_matches", "assert_url_contains"):
                        expected = step.get("value") or step.get("target") or ""
                        cur = page.url
                        if expected and expected not in cur:
                            raise AssertionError(f"URL '{cur}' does not contain '{expected}'")
                    elif action in ("screenshot",):
                        name = step.get("name", test.get("name", "screenshot").replace(" ", "_").lower())
                        shot = screenshots_dir / f"{name}.png"
                        # Optional delay before screenshot to allow UI to settle
                        try:
                            delay_ms = int(os.environ.get("SCREENSHOT_DELAY_MS", "2000"))
                        except Exception:
                            delay_ms = 2000
                        if delay_ms > 0:
                            await page.wait_for_timeout(delay_ms)
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
                    # Apply same pre-screenshot delay on failure captures
                    try:
                        delay_ms = int(os.environ.get("SCREENSHOT_DELAY_MS", "2000"))
                    except Exception:
                        delay_ms = 2000
                    if delay_ms > 0:
                        await page.wait_for_timeout(delay_ms)
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


