#!/usr/bin/env python3
"""Lightweight eval harness for the Vibecoding Jeopardy board.

For every *real* card in data/cards.csv it:
  1. starts a timer,
  2. runs the card's known-good prompt (the `answer` column) with
     `agency copilot -p "<prompt>" --yolo --mcp ...`,
  3. stops the timer, and
  4. asks `agency copilot` (as a light judge, no rubric) whether the output
     actually satisfied the card's goal (the `question` column).

A card PASSES only if it finished in under PASS_SECONDS *and* the judge says the
goal was met. Failures are labelled as either `time` or `answer` so you can tell
a slow prompt from a wrong one.

Hidden 600-point canary rows and blank "Coming soon" placeholders are skipped.

Nothing here needs pip packages — standard library only.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config — adjust these to match your Agency install.
# --------------------------------------------------------------------------- #
AGENCY_CMD = ["agency", "copilot"]          # base launcher
YOLO_FLAG = "--yolo"                          # skip approval prompts so nothing hangs
MCP_SERVERS = [                               # passed as repeated `--mcp <name>`
    "graph", "powerbi", "teams", "sharepoint", "onedrive",
    "mail", "calendar", "kusto", "workiq", "word",
]
PASS_SECONDS = 120                            # a card must finish in under 2 minutes
HIDDEN_POINTS = "600"                         # canary rows — never run these

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "cards.csv"
RESULTS_PATH = Path(__file__).resolve().parent / "results.json"

# --------------------------------------------------------------------------- #


@dataclass
class Result:
    category: str
    points: str
    question: str
    elapsed_sec: float
    status: str          # "pass" | "fail"
    failure_type: str    # "" | "time" | "answer"
    reason: str


def load_cards(csv_path: Path) -> list[dict]:
    """Return only runnable cards (skip canary rows and blank placeholders)."""
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    runnable = []
    for row in rows:
        points = (row.get("points") or "").strip()
        question = (row.get("question") or "").strip()
        answer = (row.get("answer") or "").strip()
        if points == HIDDEN_POINTS:
            continue          # hidden prompt-injection canary — do not execute
        if not question or not answer:
            continue          # "Coming soon" placeholder
        runnable.append(row)
    return runnable


def mcp_flags() -> list[str]:
    flags: list[str] = []
    for server in MCP_SERVERS:
        flags += ["--mcp", server]
    return flags


def run_agent(prompt: str, timeout: int, dry_run: bool) -> tuple[float, str, bool]:
    """Run the prompt. Returns (elapsed_seconds, output, timed_out)."""
    cmd = AGENCY_CMD + ["-p", prompt, YOLO_FLAG] + mcp_flags()
    if dry_run:
        print("    [dry-run] " + " ".join(_show(c) for c in cmd))
        return 0.0, "(dry-run: not executed)", False

    start = time.perf_counter()          # start timer right before initializing
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.perf_counter() - start
        output = (proc.stdout or "") + (proc.stderr or "")
        return elapsed, output.strip(), False
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start
        return elapsed, "", True
    except FileNotFoundError:
        raise SystemExit(
            f"Could not find '{AGENCY_CMD[0]}'. Edit AGENCY_CMD/flags at the top "
            f"of {Path(__file__).name} to match your Agency install."
        )


JUDGE_TIMEOUT = 120


def judge(question: str, prompt: str, output: str, dry_run: bool) -> tuple[bool, str]:
    """Light LLM-as-judge using agency itself. Returns (passed, reason)."""
    if dry_run:
        return True, "(dry-run: not judged)"

    clipped = output[:6000]
    judge_prompt = (
        "You are strictly grading whether an AI agent achieved a goal.\n"
        f"GOAL (what the user actually wanted): {question}\n"
        f"INSTRUCTION the agent was given: {prompt}\n"
        "AGENT OUTPUT (between the lines):\n"
        "-----\n"
        f"{clipped}\n"
        "-----\n"
        "Did the agent adequately and correctly achieve the GOAL? Be strict: an "
        "error, refusal, empty answer, or wrong result is a fail. Reply with ONLY "
        'a compact JSON object: {"verdict": "pass" or "fail", "reason": "one short sentence"}.'
    )
    cmd = AGENCY_CMD + ["-p", judge_prompt, YOLO_FLAG]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=JUDGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "judge timed out"
    text = (proc.stdout or "") + (proc.stderr or "")
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return False, "judge returned no verdict (treated as fail)"
    try:
        verdict = json.loads(match.group(0))
    except json.JSONDecodeError:
        return False, "judge verdict not valid JSON (treated as fail)"
    passed = str(verdict.get("verdict", "")).lower().strip() == "pass"
    return passed, str(verdict.get("reason", "")).strip()


def evaluate(card: dict, pass_seconds: int, dry_run: bool) -> Result:
    question = card["question"].strip()
    prompt = card["answer"].strip()

    elapsed, output, timed_out = run_agent(prompt, timeout=pass_seconds, dry_run=dry_run)

    if timed_out or elapsed > pass_seconds:
        return Result(
            category=card["category"], points=card["points"], question=question,
            elapsed_sec=round(elapsed, 1), status="fail", failure_type="time",
            reason=f"exceeded {pass_seconds}s limit",
        )

    if not dry_run and not output:
        return Result(
            category=card["category"], points=card["points"], question=question,
            elapsed_sec=round(elapsed, 1), status="fail", failure_type="answer",
            reason="agent produced no output",
        )

    passed, reason = judge(question, prompt, output, dry_run)
    if passed:
        return Result(
            category=card["category"], points=card["points"], question=question,
            elapsed_sec=round(elapsed, 1), status="pass", failure_type="", reason=reason,
        )
    return Result(
        category=card["category"], points=card["points"], question=question,
        elapsed_sec=round(elapsed, 1), status="fail", failure_type="answer", reason=reason,
    )


def _show(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def _tag(result: Result) -> str:
    if result.status == "pass":
        return "PASS"
    return "FAIL-TIME" if result.failure_type == "time" else "FAIL-ANSWER"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Jeopardy prompt evals.")
    parser.add_argument("--csv", type=Path, default=CSV_PATH, help="path to cards.csv")
    parser.add_argument("--pass-seconds", type=int, default=PASS_SECONDS,
                        help="max seconds for a card to still pass (default 120)")
    parser.add_argument("--only", help="only run cards whose category contains this text")
    parser.add_argument("--limit", type=int, help="run at most N cards")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the commands without calling agency")
    args = parser.parse_args()

    cards = load_cards(args.csv)
    if args.only:
        cards = [c for c in cards if args.only.lower() in c["category"].lower()]
    if args.limit:
        cards = cards[: args.limit]

    if not cards:
        print("No runnable cards found.")
        return 1

    print(f"Running {len(cards)} card(s) from {args.csv}")
    print(f"Pass threshold: < {args.pass_seconds}s and judge says goal met\n")

    results: list[Result] = []
    for index, card in enumerate(cards, start=1):
        print(f"[{index}/{len(cards)}] {card['category']} {card['points']} — "
              f"{card['question'][:70]}")
        result = evaluate(card, args.pass_seconds, args.dry_run)
        results.append(result)
        print(f"    -> {_tag(result)}  ({result.elapsed_sec}s)  {result.reason}\n")

    passed = sum(1 for r in results if r.status == "pass")
    fail_time = sum(1 for r in results if r.failure_type == "time")
    fail_answer = sum(1 for r in results if r.failure_type == "answer")

    print("=" * 68)
    print(f"{'CARD':<34}{'RESULT':<13}{'TIME':>8}")
    print("-" * 68)
    for r in results:
        label = f"{r.category} {r.points}"
        print(f"{label:<34}{_tag(r):<13}{r.elapsed_sec:>7}s")
    print("-" * 68)
    print(f"Passed {passed}/{len(results)}  |  time-fails: {fail_time}  |  answer-fails: {fail_answer}")

    RESULTS_PATH.write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
    )
    print(f"\nWrote detailed results to {RESULTS_PATH}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
