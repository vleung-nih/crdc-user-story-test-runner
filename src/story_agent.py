import json
import boto3


def build_prompt(story_text: str, base_url: str, inventory: dict | None = None) -> str:
    return (
        "You are a senior QA engineer. Convert the following user story into a concise suite of executable UI tests.\n"
        "Output a JSON array of test cases ONLY, no prose.\n\n"
        f"Base URL: {base_url}\n\n"
        "User Story:\n" + story_text + "\n\n"
        + ("Known elements on page (use these to prefer real selectors):\n" + json.dumps(inventory, indent=2) + "\n\n" if inventory else "")
        + "Test case schema (strict):\n"
        "[\n"
        "  {\n"
        "    \"name\": \"Short, action-oriented name\",\n"
        "    \"steps\": [\n"
        "      { \"action\": \"login_via_login_gov\", \"username_env\": \"LOGIN_USERNAME\", \"password_env\": \"LOGIN_PASSWORD\", \"totp_env\": \"TOTP_SECRET\" },\n"
        "      { \"action\": \"screenshot\", \"name\": \"after-login\" }\n"
        "    ]\n"
        "  }\n"
        "]\n\n"
        "Rules:\n"
        "- Prefer stable selectors: data-testid, role, label, id; fallback to text.\n"
        "- Keep tests independent; each starts with navigate unless using the consolidated login_via_login_gov action.\n"
        "- Use relative URLs when under base URL.\n"
        "- Avoid placeholders; use fill_env for credentials and login_via_login_gov for reliability.\n"
        "- Do NOT invent testids. If a data-testid from Known elements exists, use it; otherwise use role+name or text.\n"
        "- Avoid CSS [text='...']; use structured targets like {type:'text',value:'...'} or data-testid form.\n"
        "- For presence-only checks use 'assert' with exists:true (or existence:true); visibility is not required.\n"
        "- Limit to 3–8 tests.\n"
    )


def bedrock_invoke_claude(prompt: str, model_id: str, region: str, verbose: bool = False) -> str:
    if verbose:
        print("\n===== Agent Prompt (to Bedrock) =====")
        print(prompt)
        print("===== End Prompt =====\n")
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ],
        "max_tokens": 2000,
    }
    client = boto3.client("bedrock-runtime", region_name=region)
    resp = client.invoke_model(
        body=json.dumps(body).encode("utf-8"),
        modelId=model_id,
        accept="application/json",
        contentType="application/json",
    )
    raw = resp["body"].read().decode("utf-8")
    parsed = json.loads(raw)
    text = ""
    if isinstance(parsed.get("content"), list):
        for item in parsed["content"]:
            if item.get("type") == "text":
                text += item.get("text", "")
    if verbose:
        print("\n===== Agent Raw Response =====")
        print(text)
        print("===== End Raw Response =====\n")
    return text.strip()


def bedrock_invoke_claude_multimodal(prompt: str, images: list[dict], model_id: str, region: str, verbose: bool = False) -> str:
    """Invoke Claude with text plus one or more images.
    images: list of {"media_type": "image/png|image/jpeg", "data_base64": "..."}
    """
    if verbose:
        print("\n===== Agent Prompt (to Bedrock) =====")
        print(prompt)
        print("(with", len(images), "image(s))")
        print("===== End Prompt =====\n")
    content = [{"type": "text", "text": prompt}]
    for img in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.get("media_type", "image/png"),
                "data": img.get("data_base64", ""),
            },
        })
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "messages": [
            {"role": "user", "content": content}
        ],
        "max_tokens": 2000,
    }
    client = boto3.client("bedrock-runtime", region_name=region)
    resp = client.invoke_model(
        body=json.dumps(body).encode("utf-8"),
        modelId=model_id,
        accept="application/json",
        contentType="application/json",
    )
    raw = resp["body"].read().decode("utf-8")
    parsed = json.loads(raw)
    text = ""
    if isinstance(parsed.get("content"), list):
        for item in parsed["content"]:
            if item.get("type") == "text":
                text += item.get("text", "")
    if verbose:
        print("\n===== Agent Raw Response =====")
        print(text)
        print("===== End Raw Response =====\n")
    return text.strip()


def coerce_to_json_array(text: str) -> list:
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    # keep only content between the first [ and last ]
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        arr = json.loads(cleaned)
        if isinstance(arr, list):
            return arr
    except Exception:
        pass
    return []


def generate_test_cases_from_story(story_text: str, base_url: str, model_id: str, region: str, verbose: bool = False) -> list:
    # Try to include latest element inventory if available
    inventory = None
    try:
        from pathlib import Path
        # Pick latest run if present
        runs = sorted((Path("data/runs").glob("run_*")), key=lambda p: p.name, reverse=True)
        for r in runs:
            inv = r / "element_inventory.json"
            if inv.exists():
                inventory = json.loads(inv.read_text(encoding="utf-8"))
                break
    except Exception:
        inventory = None
    prompt = build_prompt(story_text, base_url, inventory)
    raw = bedrock_invoke_claude(prompt, model_id=model_id, region=region, verbose=verbose)
    tests = coerce_to_json_array(raw)
    if verbose:
        print("===== Parsed Test Cases (JSON) =====")
        try:
            print(json.dumps(tests, indent=2))
        except Exception:
            print(tests)
        print("===== End Parsed Test Cases =====")
    return tests


