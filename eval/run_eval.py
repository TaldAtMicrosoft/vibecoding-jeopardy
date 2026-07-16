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
import os
import re
import subprocess
import sys
import tempfile
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
PASS_SECONDS = 120                            # a card must finish in under 2 minutes to pass
KILL_SECONDS = 180                            # but let it keep running up to 3 min before killing
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


def _kill_tree(pid: int) -> None:
    """Kill a process and ALL its descendants.

    Agency spawns a grandchild engine (copilot) that keeps the output pipe
    open, so killing only the direct child leaves it running and the parent
    waiting forever. taskkill /T (Windows) / killpg (POSIX) take out the tree.
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            os.killpg(os.getpgid(pid), 9)
    except Exception:
        pass


def _run_capture(cmd: list[str], timeout: int) -> tuple[float, str, bool]:
    """Run cmd with a HARD wall-clock timeout. Returns (elapsed, output, timed_out).

    Output is streamed to a temp file (not a pipe) so a huge/slow run can never
    deadlock on a full pipe buffer, and on timeout the whole process tree is
    killed so we actually regain control instead of blocking on a grandchild.
    """
    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    tmp = tempfile.NamedTemporaryFile(
        mode="w+", encoding="utf-8", errors="replace",
        suffix=".log", delete=False,
    )
    start = time.perf_counter()          # start timer right before initializing
    timed_out = False
    try:
        proc = subprocess.Popen(cmd, stdout=tmp, stderr=subprocess.STDOUT, **popen_kwargs)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        elapsed = time.perf_counter() - start
        tmp.flush()
        tmp.seek(0)
        output = tmp.read()
    finally:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return elapsed, output.strip(), timed_out


def run_agent(prompt: str, timeout: int, dry_run: bool) -> tuple[float, str, bool]:
    """Run the prompt. Returns (elapsed_seconds, output, timed_out)."""
    cmd = AGENCY_CMD + ["-p", prompt, YOLO_FLAG] + mcp_flags()
    if dry_run:
        print("    [dry-run] " + " ".join(_show(c) for c in cmd))
        return 0.0, "(dry-run: not executed)", False
    if not _agency_exists():
        raise SystemExit(
            f"Could not find '{AGENCY_CMD[0]}'. Edit AGENCY_CMD/flags at the top "
            f"of {Path(__file__).name} to match your Agency install."
        )
    return _run_capture(cmd, timeout)


def _agency_exists() -> bool:
    from shutil import which
    return which(AGENCY_CMD[0]) is not None


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
    _, text, timed_out = _run_capture(cmd, JUDGE_TIMEOUT)
    if timed_out:
        return False, "judge timed out"
    # Agency prints startup noise containing braces (e.g. "mcp{bluebird}"), so
    # match a flat JSON object that actually contains our "verdict" key rather
    # than greedily spanning from the first brace to the last.
    candidates = re.findall(r'\{[^{}]*"verdict"[^{}]*\}', text, re.DOTALL)
    if not candidates:
        return False, "judge returned no verdict (treated as fail)"
    try:
        verdict = json.loads(candidates[-1])
    except json.JSONDecodeError:
        return False, "judge verdict not valid JSON (treated as fail)"
    passed = str(verdict.get("verdict", "")).lower().strip() == "pass"
    return passed, str(verdict.get("reason", "")).strip()


def evaluate(card: dict, pass_seconds: int, kill_seconds: int, dry_run: bool) -> Result:
    question = card["question"].strip()
    prompt = card["answer"].strip()

    # Let the agent run up to kill_seconds so we capture its real duration,
    # but it only PASSES if it finished within pass_seconds.
    elapsed, output, timed_out = run_agent(prompt, timeout=kill_seconds, dry_run=dry_run)

    if timed_out or elapsed > pass_seconds:
        reason = (
            f"killed after {kill_seconds}s" if timed_out
            else f"took {elapsed:.1f}s, exceeded {pass_seconds}s limit"
        )
        return Result(
            category=card["category"], points=card["points"], question=question,
            elapsed_sec=round(elapsed, 1), status="fail", failure_type="time",
            reason=reason,
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
    try:
        sys.stdout.reconfigure(line_buffering=True)  # stream per-card lines live
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Run the Jeopardy prompt evals.")
    parser.add_argument("--csv", type=Path, default=CSV_PATH, help="path to cards.csv")
    parser.add_argument("--pass-seconds", type=int, default=PASS_SECONDS,
                        help="max seconds for a card to still pass (default 120)")
    parser.add_argument("--kill-seconds", type=int, default=KILL_SECONDS,
                        help="hard timeout before the agent is killed (default 300)")
    parser.add_argument("--only", help="only run cards whose category contains this text")
    parser.add_argument("--limit", type=int, help="run at most N cards")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the commands without calling agency")
    parser.add_argument("--mcp", help="comma-separated MCP servers to launch for this "
                        "run, overriding the default list (e.g. --mcp sharepoint)")
    args = parser.parse_args()

    if args.mcp is not None:
        global MCP_SERVERS
        MCP_SERVERS = [s.strip() for s in args.mcp.split(",") if s.strip()]

    cards = load_cards(args.csv)
    if args.only:
        cards = [c for c in cards if args.only.lower() in c["category"].lower()]
    if args.limit:
        cards = cards[: args.limit]

    if not cards:
        print("No runnable cards found.")
        return 1

    print(f"Running {len(cards)} card(s) from {args.csv}")
    print(f"Pass threshold: < {args.pass_seconds}s and judge says goal met "
          f"(killed at {args.kill_seconds}s)\n")

    results: list[Result] = []
    for index, card in enumerate(cards, start=1):
        print(f"[{index}/{len(cards)}] {card['category']} {card['points']} — "
              f"{card['question'][:70]}")
        result = evaluate(card, args.pass_seconds, args.kill_seconds, args.dry_run)
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
