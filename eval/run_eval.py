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
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config — adjust these to match your Agency install.
# --------------------------------------------------------------------------- #
AGENCY_CMD = ["agency", "copilot"]          # base launcher
YOLO_FLAG = "--yolo"                          # skip approval prompts so nothing hangs
MCP_SERVERS = [                               # passed as repeated `--mcp <name>`
    "sharepoint",   # Visualize 100/200/300, Productionalize 100 (Contract Review list)
    "workiq",       # Get Data 200 (Outlook Sent items)
]
PASS_SECONDS = 200                            # max *prompt-execution* time (cold start excluded) to pass
KILL_SECONDS = 260                            # hard wall-clock timeout (cold start + exec) before killing
HIDDEN_POINTS = "600"                         # canary rows — never run these
DEFAULT_JOBS = 0                              # 0 == run every card concurrently (one worker each)

# Structured event stream. Runs still use --yolo (every action auto-approved so
# nothing hangs and timing stays clean); JSON output lets us COUNT how many
# tool calls WOULD have prompted the user for approval in a normal session, so
# we can see which prompts are onerous without changing pass/fail behaviour.
OUTPUT_FORMAT = "json"
# Tools that never trigger an approval prompt (read-only inspection / internal
# control). Everything else (shell, file writes/edits, fetch, MCP tool calls…)
# is counted as a would-be approval. This is a heuristic for prompt onerousness.
NO_APPROVAL_TOOLS = {
    "view", "grep", "glob", "ls", "read", "read_file", "cat", "head", "tail",
    "task_complete", "todo_write", "todo_read", "think", "plan",
}

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "cards.csv"
RESULTS_PATH = Path(__file__).resolve().parent / "results.json"
LOGS_DIR = Path(__file__).resolve().parent / "logs"      # per-card JSONL transcripts
PROJECT_ROOT = CSV_PATH.resolve().parent.parent          # repo root (the agent runs here)

# Files a card's prompt is expected to LEAVE ON DISK. After the run, every listed
# file must exist for the card to pass — a hard artifact check that is independent
# of whatever the judge reads from the transcript. Each file is deleted before the
# run so we prove THIS run actually (re)created it.
EXPECTED_ARTIFACTS: dict[tuple[str, str], list[str]] = {
    ("Productionalize", "100"): ["plan.md"],
    ("Productionalize", "200"): ["build_dashboard.py", "contract_deadline_dashboard.html"],
    ("Productionalize", "300"): ["dashboard/index.html", "dashboard/README.md",
                                 "dashboard/test_dashboard.py"],
}


def _expected_artifacts(card: dict) -> list[Path]:
    key = (card.get("category", "").strip(), str(card.get("points", "")).strip())
    return [PROJECT_ROOT / name for name in EXPECTED_ARTIFACTS.get(key, [])]

# stdout is shared across worker threads; serialize prints so lines don't interleave.
_PRINT_LOCK = threading.Lock()


def log(msg: str = "") -> None:
    with _PRINT_LOCK:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            # Judge output can contain characters (e.g. U+2212 minus) that the
            # active console codepage (cp1252 on Windows) can't encode. Never let
            # a stray glyph crash the whole run before the summary table prints.
            enc = (sys.stdout.encoding or "utf-8")
            print(msg.encode(enc, "replace").decode(enc), flush=True)

# --------------------------------------------------------------------------- #


@dataclass
class Result:
    category: str
    points: str
    question: str
    elapsed_sec: float           # total wall-clock (cold start + execution)
    coldstart_sec: float | None  # agency launch -> engine boots ("Session file detected")
    exec_sec: float | None       # prompt-execution time only (None = never booted)
    status: str          # "pass" | "fail"
    failure_type: str    # "" | "time" | "answer"
    reason: str
    approvals: int = 0            # tool calls that WOULD have prompted for approval
    approvals_unique: int = 0     # distinct approvals (models "approve & remember")
    tool_calls: int = 0           # total tool calls the agent made
    approvals_by_tool: dict = field(default_factory=dict)


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


# When the harness is launched from *inside* an `agency copilot` session, these
# vars are exported into our environment and would be inherited by every child
# `agency copilot` we spawn. A child that inherits them re-attaches to the parent
# session instead of starting a clean, isolated headless run — so it never returns
# (killed at KILL_SECONDS) and, run concurrently, the nested sessions fight over
# the same MCP proxies. Strip them so each run starts fresh.
INHERITED_SESSION_VARS = (
    "AGENCY_SESSION_ID",
    "AGENCY_SESSION_SUBPROCESS",
    "AGENCY_LOG_SESSION_DIR",
    "AGENCY_OPERATION_ID",
    "COPILOT_AGENT_SESSION_ID",
)


def _clean_env() -> dict:
    """A copy of the current env with inherited agency/session vars removed."""
    env = os.environ.copy()
    for var in INHERITED_SESSION_VARS:
        env.pop(var, None)
    return env


def _run_capture(cmd: list[str], timeout: int) -> tuple[float, str, bool]:
    """Run cmd with a HARD wall-clock timeout. Returns (elapsed, output, timed_out).

    Output is streamed to a temp file (not a pipe) so a huge/slow run can never
    deadlock on a full pipe buffer, and on timeout the whole process tree is
    killed so we actually regain control instead of blocking on a grandchild.
    """
    popen_kwargs: dict = {"env": _clean_env()}
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
    cmd = AGENCY_CMD + ["-p", prompt, YOLO_FLAG, "--output-format", OUTPUT_FORMAT] + mcp_flags()
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


_LOGDIR_RE = re.compile(r"Log directory:\s*(.+?session_\S+)")
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z")
_BOOT_MARKER = "Session file detected"          # engine has booted and picked up the prompt


def _parse_timing(output: str) -> tuple[float | None, float | None]:
    """Return (coldstart_sec, exec_sec) parsed from an agency run's captured output.

    Every agency run prints "Log directory: ...session_<id>" up front; that dir
    holds a timestamped debug log. Cold start = first log line -> the engine
    booting and picking up the prompt (``Session file detected``); execution =
    that boot point -> the last log line. This lets us judge a card on the time
    it actually spent running the prompt, not the one-time cold start that real
    users pay only once. Returns (cold, None) if the engine never booted (run
    stayed stuck in MCP initialization), or (None, None) if timing is unavailable.
    """
    m = _LOGDIR_RE.search(output or "")
    if not m:
        return None, None
    try:
        logs = sorted(Path(m.group(1).strip()).glob("agency_copilot_*.log"))
        if not logs:
            return None, None
        text = logs[0].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None

    start = boot = last = None
    for line in text.splitlines():
        tm = _TS_RE.search(line)
        if not tm:
            continue
        t = datetime.fromisoformat(tm.group(1))
        if start is None:
            start = t
        last = t
        if boot is None and _BOOT_MARKER in line:
            boot = t
    if start is None:
        return None, None
    if boot is None:
        return None, None                        # engine never booted -> no execution
    cold = (boot - start).total_seconds()
    exec_sec = max(0.0, (last - boot).total_seconds())
    return cold, exec_sec


def _card_slug(card: dict) -> str:
    raw = f"{card['category']}_{card['points']}"
    return re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()


def _needs_approval(tool_name: str) -> bool:
    """A tool call the user would have to approve in a normal (non-yolo) session."""
    return bool(tool_name) and tool_name not in NO_APPROVAL_TOOLS


def _approval_key(tool_name: str, arguments: dict) -> str:
    """Model Copilot's "approve & remember" so repeats of the same action count once.

    Shell approvals are remembered per command, file/URL approvals per target, and
    everything else per tool.
    """
    args = arguments or {}
    target = args.get("command") or args.get("path") or args.get("url") or ""
    return f"{tool_name}|{target}"


def _analyze_events(output: str) -> dict:
    """Parse the JSONL event stream: count would-be approvals and rebuild the
    human-readable answer text (assistant messages) for the judge."""
    tool_calls = 0
    approvals = 0
    by_tool: dict[str, int] = {}
    keys: set[str] = set()
    messages: list[str] = []

    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        data = event.get("data") or {}
        if etype == "tool.execution_start":
            tool_calls += 1
            name = data.get("toolName", "")
            if _needs_approval(name):
                approvals += 1
                by_tool[name] = by_tool.get(name, 0) + 1
                keys.add(_approval_key(name, data.get("arguments") or {}))
        elif etype == "assistant.message":
            content = (data.get("content") or "").strip()
            if content:
                messages.append(content)

    return {
        "tool_calls": tool_calls,
        "approvals": approvals,
        "approvals_unique": len(keys),
        "approvals_by_tool": by_tool,
        "final_text": "\n".join(messages),
    }


def run_card(card: dict, kill_seconds: int, dry_run: bool) -> dict:
    """Phase 1: execute a card's prompt. Returns a raw run record (no judging yet)."""
    prompt = card["answer"].strip()
    artifacts = _expected_artifacts(card)
    if not dry_run:
        for path in artifacts:               # start clean so we prove THIS run made them
            try:
                path.unlink()
            except OSError:
                pass
    elapsed, output, timed_out = run_agent(prompt, timeout=kill_seconds, dry_run=dry_run)
    coldstart, exec_sec = (None, None) if dry_run else _parse_timing(output)

    analysis = {"tool_calls": 0, "approvals": 0, "approvals_unique": 0,
                "approvals_by_tool": {}, "final_text": ""}
    if not dry_run and output:
        analysis = _analyze_events(output)
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            (LOGS_DIR / f"{_card_slug(card)}.jsonl").write_text(
                output, encoding="utf-8", errors="replace"
            )
        except OSError:
            pass

    missing_artifacts = ([] if dry_run
                         else [str(p.relative_to(PROJECT_ROOT)) for p in artifacts if not p.exists()])
    return {"card": card, "elapsed": elapsed, "output": output,
            "timed_out": timed_out, "coldstart": coldstart, "exec": exec_sec,
            "analysis": analysis, "missing_artifacts": missing_artifacts,
            "final_text": analysis.get("final_text") or output}


def judge_run(run: dict, pass_seconds: int, kill_seconds: int, dry_run: bool) -> Result:
    """Phase 2: gate on *execution* time (cold start excluded), then ask the judge."""
    card = run["card"]
    elapsed, output, timed_out = run["elapsed"], run["output"], run["timed_out"]
    coldstart, exec_sec = run.get("coldstart"), run.get("exec")
    analysis = run.get("analysis") or {}
    judge_text = run.get("final_text") or output
    question = card["question"].strip()
    prompt = card["answer"].strip()

    base = dict(
        category=card["category"], points=card["points"], question=question,
        elapsed_sec=round(elapsed, 1),
        coldstart_sec=round(coldstart, 1) if coldstart is not None else None,
        exec_sec=round(exec_sec, 1) if exec_sec is not None else None,
        approvals=analysis.get("approvals", 0),
        approvals_unique=analysis.get("approvals_unique", 0),
        tool_calls=analysis.get("tool_calls", 0),
        approvals_by_tool=analysis.get("approvals_by_tool", {}),
    )

    if timed_out:
        return Result(**base, status="fail", failure_type="time",
                      reason=f"killed after {kill_seconds}s (never returned)")

    # Engine never booted -> the run stayed stuck in MCP init and never actually
    # executed the prompt. That's a time failure regardless of the exec gate.
    if not dry_run and exec_sec is None:
        return Result(**base, status="fail", failure_type="time",
                      reason=f"engine never started the prompt (stuck in MCP init) "
                             f"after {elapsed:.1f}s total")

    gate = exec_sec if exec_sec is not None else elapsed
    if not dry_run and gate > pass_seconds:
        cold_note = f", {coldstart:.0f}s cold start" if coldstart is not None else ""
        return Result(**base, status="fail", failure_type="time",
                      reason=f"prompt execution {gate:.1f}s exceeded {pass_seconds}s limit "
                             f"(total {elapsed:.1f}s{cold_note})")

    if not dry_run and not output:
        return Result(**base, status="fail", failure_type="answer",
                      reason="agent produced no output")

    missing = run.get("missing_artifacts") or []
    if not dry_run and missing:
        return Result(**base, status="fail", failure_type="answer",
                      reason=f"expected artifact(s) not created: {', '.join(missing)}")

    passed, reason = judge(question, prompt, judge_text, dry_run)
    return Result(**base, status="pass" if passed else "fail",
                  failure_type="" if passed else "answer", reason=reason)


def _show(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def _tag(result: Result) -> str:
    if result.status == "pass":
        return "PASS"
    return "FAIL-TIME" if result.failure_type == "time" else "FAIL-ANSWER"


# ANSI colors for the execution-time bands.
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_ORANGE = "\033[38;5;208m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _exec_color(exec_sec: float | None) -> str:
    """Green <100s, yellow <120s, orange <160s, red >=160s or never-booted."""
    if exec_sec is None:
        return _RED
    if exec_sec < 100:
        return _GREEN
    if exec_sec < 120:
        return _YELLOW
    if exec_sec < 160:
        return _ORANGE
    return _RED


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Jeopardy prompt evals.")
    parser.add_argument("--csv", type=Path, default=CSV_PATH, help="path to cards.csv")
    parser.add_argument("--pass-seconds", type=int, default=PASS_SECONDS,
                        help="max prompt-execution seconds (cold start excluded) to "
                             "still pass (default 200)")
    parser.add_argument("--kill-seconds", type=int, default=KILL_SECONDS,
                        help="hard wall-clock timeout before the agent is killed (default 260)")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                        help="max concurrent agent runs (0 = one per card, all at once)")
    parser.add_argument("--only", help="only run cards whose category contains this text")
    parser.add_argument("--limit", type=int, help="run at most N cards")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the commands without calling agency")
    parser.add_argument("--mcp", help="comma-separated MCP servers to launch for this "
                        "run, overriding the default list (e.g. --mcp sharepoint)")
    args = parser.parse_args()

    if os.name == "nt":
        os.system("")  # enable ANSI VT processing in the Windows console
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.mcp is not None:
        global MCP_SERVERS
        MCP_SERVERS = [s.strip() for s in args.mcp.split(",") if s.strip()]

    cards = load_cards(args.csv)
    if args.only:
        needle = args.only.lower()
        cards = [c for c in cards
                 if needle in c["category"].lower()
                 or needle in f"{c['category']} {c['points']}".lower()]
    if args.limit:
        cards = cards[: args.limit]

    if not cards:
        log("No runnable cards found.")
        return 1

    jobs = args.jobs if args.jobs and args.jobs > 0 else len(cards)
    jobs = min(jobs, len(cards))

    log(f"Running {len(cards)} card(s) from {args.csv}")
    log(f"Pass threshold: prompt execution < {args.pass_seconds}s (cold start excluded) "
        f"and judge says goal met (hard kill at {args.kill_seconds}s)")
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
            if r["timed_out"]:
                note = "killed"
            elif r.get("exec") is not None:
                note = f"{r['elapsed']:.1f}s total ({r.get('coldstart') or 0:.0f}s cold + {r['exec']:.1f}s exec)"
            else:
                note = f"{r['elapsed']:.1f}s (never booted)"
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
                f"{_tag(result)}  [{result.approvals} approvals]  {result.reason}")
    judge_secs = time.perf_counter() - judge_start
    wall_total = time.perf_counter() - wall_start
    log(f"Phase 2 done in {judge_secs:.1f}s wall-clock.\n")

    results = [results_by_index[i] for i in range(len(cards))]

    passed = sum(1 for r in results if r.status == "pass")
    fail_time = sum(1 for r in results if r.failure_type == "time")
    fail_answer = sum(1 for r in results if r.failure_type == "answer")

    log("=" * 78)
    log(f"  {'CARD':<28}{'RESULT':<13}{'COLD':>6}{'EXEC':>8}{'TOTAL':>8}{'APPROVE':>9}")
    log("-" * 78)
    for r in results:
        label = f"{r.category} {r.points}"
        cold = f"{r.coldstart_sec:.0f}s" if r.coldstart_sec is not None else "-"
        ex_plain = f"{r.exec_sec:.1f}s" if r.exec_sec is not None else "-"
        color = _exec_color(r.exec_sec)
        dot = f"{color}\u25CF{_RESET}"
        ex_cell = f"{color}{ex_plain:>8}{_RESET}"
        appr = f"{r.approvals}/{r.approvals_unique}"
        log(f"{dot} {label:<28}{_tag(r):<13}{cold:>6}{ex_cell}{r.elapsed_sec:>7}s{appr:>9}")
    log("-" * 78)
    log(f"Exec band:  {_GREEN}\u25CF <100s{_RESET}   {_YELLOW}\u25CF <120s{_RESET}   "
        f"{_ORANGE}\u25CF <160s{_RESET}   {_RED}\u25CF >=160s / never-booted{_RESET}")
    log(f"Passed {passed}/{len(results)}  |  time-fails: {fail_time}  |  answer-fails: {fail_answer}")
    log(f"Gate: prompt execution < {args.pass_seconds}s (cold start excluded)")

    total_appr = sum(r.approvals for r in results)
    total_uniq = sum(r.approvals_unique for r in results)
    log(f"Approvals: {total_appr} total ({total_uniq} unique) would be requested across "
        f"{len(results)} card(s) — APPROVE column is total/unique per card.")
    heaviest = sorted(results, key=lambda r: r.approvals, reverse=True)[:3]
    if heaviest and heaviest[0].approvals:
        tops = ", ".join(f"{r.category} {r.points} ({r.approvals})" for r in heaviest if r.approvals)
        log(f"Most approval-heavy prompts: {tops}")
    log(f"Wall-clock: {wall_total:.1f}s total "
        f"(run {run_secs:.1f}s + judge {judge_secs:.1f}s)")

    RESULTS_PATH.write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
    )
    log(f"\nWrote detailed results to {RESULTS_PATH}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
