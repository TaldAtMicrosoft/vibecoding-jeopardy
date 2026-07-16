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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
DEFAULT_JOBS = 0                              # 0 == run every card concurrently (one worker each)

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "cards.csv"
RESULTS_PATH = Path(__file__).resolve().parent / "results.json"

# stdout is shared across worker threads; serialize prints so lines don't interleave.
_PRINT_LOCK = threading.Lock()


def log(msg: str = "") -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)

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
        log("    [dry-run] " + " ".join(_show(c) for c in cmd))
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


def run_card(card: dict, kill_seconds: int, dry_run: bool) -> dict:
    """Phase 1: execute a card's prompt. Returns a raw run record (no judging yet)."""
    prompt = card["answer"].strip()
    elapsed, output, timed_out = run_agent(prompt, timeout=kill_seconds, dry_run=dry_run)
    return {"card": card, "elapsed": elapsed, "output": output, "timed_out": timed_out}


def judge_run(run: dict, pass_seconds: int, kill_seconds: int, dry_run: bool) -> Result:
    """Phase 2: apply the time gate, then (if in time) ask the judge."""
    card = run["card"]
    elapsed, output, timed_out = run["elapsed"], run["output"], run["timed_out"]
    question = card["question"].strip()
    prompt = card["answer"].strip()

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
    status = "pass" if passed else "fail"
    return Result(
        category=card["category"], points=card["points"], question=question,
        elapsed_sec=round(elapsed, 1), status=status,
        failure_type="" if passed else "answer", reason=reason,
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
    parser.add_argument("--kill-seconds", type=int, default=KILL_SECONDS,
                        help="hard timeout before the agent is killed (default 180)")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                        help="max concurrent agent runs (0 = one per card, all at once)")
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
        log("No runnable cards found.")
        return 1

    jobs = args.jobs if args.jobs and args.jobs > 0 else len(cards)
    jobs = min(jobs, len(cards))

    log(f"Running {len(cards)} card(s) from {args.csv}")
    log(f"Pass threshold: < {args.pass_seconds}s and judge says goal met "
        f"(killed at {args.kill_seconds}s)")
    log(f"Concurrency: up to {jobs} agent run(s) at a time\n")

    wall_start = time.perf_counter()

    # --- Phase 1: run every card's prompt concurrently -------------------- #
    log(f"Phase 1/2 - launching {len(cards)} agent run(s)...")
    runs: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = {pool.submit(run_card, card, args.kill_seconds, args.dry_run): i
                for i, card in enumerate(cards)}
        done = 0
        for fut in as_completed(futs):
            i = futs[fut]
            runs[i] = fut.result()
            done += 1
            card = cards[i]
            r = runs[i]
            note = "killed" if r["timed_out"] else f"{r['elapsed']:.1f}s"
            log(f"  [run {done}/{len(cards)}] {card['category']} {card['points']} - {note}")
    run_secs = time.perf_counter() - wall_start
    log(f"Phase 1 done in {run_secs:.1f}s wall-clock.\n")

    # --- Phase 2: judge every run concurrently ---------------------------- #
    log(f"Phase 2/2 - judging {len(cards)} run(s)...")
    judge_start = time.perf_counter()
    results_by_index: dict[int, Result] = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = {pool.submit(judge_run, runs[i], args.pass_seconds,
                            args.kill_seconds, args.dry_run): i
                for i in range(len(cards))}
        done = 0
        for fut in as_completed(futs):
            i = futs[fut]
            result = fut.result()
            results_by_index[i] = result
            done += 1
            log(f"  [judge {done}/{len(cards)}] {result.category} {result.points} - "
                f"{_tag(result)}  {result.reason}")
    judge_secs = time.perf_counter() - judge_start
    wall_total = time.perf_counter() - wall_start
    log(f"Phase 2 done in {judge_secs:.1f}s wall-clock.\n")

    results = [results_by_index[i] for i in range(len(cards))]

    passed = sum(1 for r in results if r.status == "pass")
    fail_time = sum(1 for r in results if r.failure_type == "time")
    fail_answer = sum(1 for r in results if r.failure_type == "answer")

    log("=" * 68)
    log(f"{'CARD':<34}{'RESULT':<13}{'TIME':>8}")
    log("-" * 68)
    for r in results:
        label = f"{r.category} {r.points}"
        log(f"{label:<34}{_tag(r):<13}{r.elapsed_sec:>7}s")
    log("-" * 68)
    log(f"Passed {passed}/{len(results)}  |  time-fails: {fail_time}  |  answer-fails: {fail_answer}")
    log(f"Wall-clock: {wall_total:.1f}s total "
        f"(run {run_secs:.1f}s + judge {judge_secs:.1f}s)")

    RESULTS_PATH.write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
    )
    log(f"\nWrote detailed results to {RESULTS_PATH}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
