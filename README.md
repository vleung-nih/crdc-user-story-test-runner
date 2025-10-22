## User Story Test Runner

### Overview
This project takes a natural-language user story as input, uses an AI agent to generate executable UI test cases, and then runs those tests with Playwright. It mirrors the skeleton of `autonomous-ui-validator` while changing the core functionality to user-story-driven testing.

### Project Structure
```
user-story-test-runner/
├── src/
│   ├── __init__.py
│   ├── run_story_agent.py      # Main CLI entrypoint
│   ├── story_agent.py          # LLM-based test case generator from user story
│   ├── runner.py               # Playwright test execution engine
│   └── tools.py                # Browser helpers
├── data/
│   └── runs/                   # Timestamped run outputs
├── docs/
│   └── user_story_example.md   # Example user story
├── requirements.txt
└── run.py
```

### Prerequisites
- Python 3.9+
- Node.js (for Playwright browsers)
- AWS credentials configured for Bedrock (to use Anthropic Claude via `boto3`)

### Install
```bash
pip install -r requirements.txt
playwright install
```

### Usage
Provide a user story (as a string or file) and a base URL. The agent will generate tests and run them.

```bash
# From project root
python run.py \
  --base-url "https://example.com" \
  --story-file docs/user_story_example.md

# Or pass inline text
python run.py --base-url "https://example.com" --story "As a user, I can log in..."
```

Key options:
- `--dry-run`: Generate test cases only, do not execute
- `--model-id`: Bedrock model id (default: `anthropic.claude-3-sonnet-20240229-v1:0`)
- `--region`: AWS region (default: `us-east-1`)
- `--verbose`: Print agent prompts, raw responses, parsed test cases, and per-step execution logs

### Credentials and TOTP/2FA support
- Set credentials as environment variables:
```bash
export LOGIN_USERNAME="alice@example.com"
export LOGIN_PASSWORD="correct horse battery staple"
```

- Use `fill_env` steps to populate them:
```json
{ "action": "fill_env", "selector": "#username", "env": "LOGIN_USERNAME" }
```
```json
{ "action": "fill_env", "selector": "#password", "env": "LOGIN_PASSWORD" }
```

- TOTP:
- `pyotp` is included. Set your secret in `TOTP_SECRET` or provide per step.
- CLI helper (matches your snippet):
```bash
python src/totp_cli.py YOUR_BASE32_SECRET
```

- New test action: `fill_totp` to populate current OTP code.
Example step:
```json
{
  "action": "fill_totp",
  "selector": "#otp",
  "env": "TOTP_SECRET"
}
```
Or inline the secret (use with caution):
```json
{
  "action": "fill_totp",
  "selector": "#otp",
  "secret": "BASE32SECRET..."
}
```

### Output
Each run creates `data/runs/run_YYYYMMDD_HHMMSS/` with:
- `test_cases.json` – generated test cases
- `results.json` – pass/fail with diagnostics
- `report.html` – simple HTML summary
- `screenshots/` – screenshots per test
- `run_log.csv` – run log index
- `archive.zip` – zipped artifacts

### Notes
- The runner supports: `navigate`, `click`, `click_text`, `fill`, `fill_env`, `fill_totp`, `login_gov`, `wait_for_url_contains`, `assert_text`, `assert_element`, `wait_for`, `screenshot`.
- The agent is prompt-engineered to prefer robust selectors but will fallback to text-based locators when needed.

### Consolidated login action
- New action: `login_via_login_gov` performs a deterministic flow:
  1) Navigate to base URL and dismiss consent
  2) Click site Login, then Login.gov
  3) Fill username/password, submit
  4) Fill TOTP (2 attempts) and handle consent “Grant” if shown

Example step:
```json
{
  "action": "login_via_login_gov",
  "username_env": "LOGIN_USERNAME",
  "password_env": "LOGIN_PASSWORD",
  "totp_env": "TOTP_SECRET"
}
```

### License
MIT


