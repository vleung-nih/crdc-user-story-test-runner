#!/usr/bin/env python3

import argparse
import asyncio
import csv
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path

from story_agent import generate_test_cases_from_story
from runner import run_test_suite


def write_html_report(results_json: dict, html_path: Path):
    passed = sum(1 for r in results_json.get("tests", []) if r.get("status") == "passed")
    failed = sum(1 for r in results_json.get("tests", []) if r.get("status") == "failed")
    total = len(results_json.get("tests", []))

    html = f"""
<html><head><title>User Story Test Report</title>
<style>
body {{ font-family: Arial, sans-serif; padding: 20px; }}
.summary {{ margin-bottom: 16px; }}
.pass {{ color: #0a7b44; }}
.fail {{ color: #b00020; }}
pre {{ background: #f6f8fa; padding: 12px; border-radius: 6px; overflow: auto; }}
</style>
</head><body>
  <h1>User Story Test Report</h1>
  <div class="summary">
    <strong>Total:</strong> {total} &nbsp; <strong class="pass">Passed:</strong> {passed} &nbsp; <strong class="fail">Failed:</strong> {failed}
  </div>
  <hr />
  {''.join(render_test_result(tr) for tr in results_json.get('tests', []))}
</body></n></html>
"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)


def render_test_result(test_result: dict) -> str:
    status_class = "pass" if test_result.get("status") == "passed" else "fail"
    name = test_result.get("name", "Unnamed Test")
    error = test_result.get("error", "")
    screenshot = test_result.get("screenshot", "")
    steps_rendered = json.dumps(test_result.get("steps", []), indent=2)
    img_tag = f"<div><img src=\"{screenshot}\" style=\"max-width: 100%; border: 1px solid #ddd;\" /></div>" if screenshot else ""
    error_block = f"<pre>{error}</pre>" if error else ""
    return f"""
  <section>
    <h3 class="{status_class}">{name} — {test_result.get('status','unknown').upper()}</h3>
    <details>
      <summary>Steps</summary>
      <pre>{steps_rendered}</pre>
    </details>
    {img_tag}
    {error_block}
  </section>
  <hr />
"""


def archive_files(zip_path: Path, files: list[Path]):
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in files:
            if f.exists():
                zf.write(f, arcname=f.name)


def log_to_csv(log_path: Path, timestamp: str, artifacts: dict):
    csv_exists = log_path.exists()
    with open(log_path, "a", newline="") as csvfile:
        writer = csv.writer(csvfile)
        if not csv_exists:
            writer.writerow(["Timestamp", "Test Cases", "Results", "Report", "Archive"])
        writer.writerow([
            timestamp,
            str(artifacts.get("test_cases")),
            str(artifacts.get("results")),
            str(artifacts.get("report")),
            str(artifacts.get("archive")),
        ])


def read_story_text(args: argparse.Namespace) -> str:
    if args.story:
        return args.story
    if args.story_file:
        return Path(args.story_file).read_text(encoding="utf-8")
    raise SystemExit("You must provide --story or --story-file")


def main():
    parser = argparse.ArgumentParser(description="User Story → Test Cases → Runner")
    parser.add_argument("--base-url", required=True, help="Base URL under test")
    parser.add_argument("--story", help="Inline user story text")
    parser.add_argument("--story-file", help="Path to user story file (md/txt)")
    parser.add_argument("--dry-run", action="store_true", help="Only generate tests, do not execute")
    parser.add_argument("--model-id", default="anthropic.claude-3-sonnet-20240229-v1:0")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--headful", action="store_true", help="Run browser headful for debugging")
    parser.add_argument("--verbose", action="store_true", help="Print full prompts, responses, and step logs")
    parser.add_argument("--repair", action="store_true", help="Enable agent-in-the-loop selector repair on failures")
    parser.add_argument("--agent-verify", action="store_true", help="Ask agent to verify post-assert state to catch false positives")

    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(f"data/runs/run_{timestamp}")
    screenshots_dir = run_dir / "screenshots"
    run_dir.mkdir(parents=True, exist_ok=True)
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    story_text = read_story_text(args)

    print("🧠 Generating test cases from user story...")
    test_cases = generate_test_cases_from_story(
        story_text=story_text,
        base_url=args.base_url,
        model_id=args.model_id,
        region=args.region,
        verbose=args.verbose,
    )

    test_cases_path = run_dir / "test_cases.json"
    with open(test_cases_path, "w", encoding="utf-8") as f:
        json.dump(test_cases, f, indent=2)
    print(f"📄 Test cases written: {test_cases_path}")

    artifacts = {"test_cases": test_cases_path}

    results_json = {"tests": []}
    if not args.dry_run:
        print("🏃 Running tests with Playwright...")
        results_json = asyncio.run(run_test_suite(
            base_url=args.base_url,
            test_cases=test_cases,
            run_dir=run_dir,
            headless=(not args.headful),
            verbose=args.verbose,
            model_id=args.model_id,
            region=args.region,
            repair=args.repair,
            agent_verify=args.agent_verify,
        ))

        results_path = run_dir / "results.json"
        with open(results_path, "w", encoding="utf-8") as f:
            json.dump(results_json, f, indent=2)
        print(f"📊 Results written: {results_path}")
        artifacts["results"] = results_path

    report_path = run_dir / "report.html"
    write_html_report(results_json, report_path)
    artifacts["report"] = report_path
    print(f"📝 HTML report: {report_path}")

    archive_path = run_dir / "archive.zip"
    archive_files(archive_path, [test_cases_path, report_path] + ([results_path] if not args.dry_run else []))
    artifacts["archive"] = archive_path
    print(f"📦 Archive: {archive_path}")

    csv_log_path = run_dir / "run_log.csv"
    log_to_csv(csv_log_path, timestamp, artifacts)

    # Final console summary
    total = len(results_json.get("tests", []))
    passed = sum(1 for r in results_json.get("tests", []) if r.get("status") == "passed")
    failed = sum(1 for r in results_json.get("tests", []) if r.get("status") == "failed")
    if total:
        print(f"✅ Done. Total: {total}, Passed: {passed}, Failed: {failed}")
    else:
        print("✅ Done. No tests executed (dry run or empty suite).")


if __name__ == "__main__":
    main()


