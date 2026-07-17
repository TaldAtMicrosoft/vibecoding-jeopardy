# Prompt evals

`run_eval.py` runs each real card's known-good prompt (the `answer` column of
`../data/cards.csv`) through **Agency Copilot**, times it, and uses a light
LLM-as-judge (Agency itself — no rubric to maintain) to decide whether the
result met the card's goal (the `question` column).

A card **passes** only if it:
1. finishes in **under 2 minutes** (120s), and
2. the judge says the goal was adequately and correctly achieved.

A slow run is **not** killed at 2 minutes — the agent keeps running (up to a
5-minute hard kill) so its real duration is recorded, but anything over 120s is
still a `time` failure.

Failures are labelled **`time`** (too slow) or **`answer`** (wrong/empty result)
so you can tell them apart.

Hidden `600`-point canary rows and blank "Coming soon" placeholders are skipped.

## Approval counting

Runs execute under `--yolo` (everything auto-approved) so timing is never
distorted by an interactive prompt. On top of that, every run is launched with
`--output-format json` and the JSONL event stream is parsed to count how many
tool executions **would** have required a user approval if the card were run
under `/yolo auto` instead of full `--yolo`.

The `APPROVE` column in the summary shows `total/unique` per card:

- **total** — every would-be approval (each `tool.execution_start` whose tool is
  not in `NO_APPROVAL_TOOLS`).
- **unique** — collapses repeats of the same tool + command/path/url, modelling
  an "approve & remember" click.

Approvals are **informational only** — they never affect pass/fail. Use them to
spot prompts that are onerous (many approvals) and worth trimming. A per-card
breakdown (`approvals_by_tool`, `tool_calls`) is written to `eval/results.json`.

The full JSONL event stream for each card is saved under `eval/logs/<card>.jsonl`
(git-ignored) for offline inspection.

## Run

```bash
python eval/run_eval.py            # run every real card
python eval/run_eval.py --dry-run  # print the agency commands without executing
python eval/run_eval.py --only "Get Data"   # one column
python eval/run_eval.py --limit 1  # smoke test the first card
```

Results are also written to `eval/results.json`. Exit code is `0` only if every
card passes.

## Configure

Edit the constants at the top of `run_eval.py` to match your Agency install:

| Constant | Meaning |
| --- | --- |
| `AGENCY_CMD` | base launcher, default `["agency", "copilot"]` |
| `YOLO_FLAG` | approval-skipping flag so runs don't hang (default `--yolo`) |
| `OUTPUT_FORMAT` | `--output-format` value, `json` so approvals can be counted |
| `NO_APPROVAL_TOOLS` | tools that auto-run and are **not** counted as approvals |
| `MCP_SERVERS` | servers passed as repeated `--mcp <name>` |
| `PASS_SECONDS` | the pass threshold (prompt execution, cold start excluded) |
| `KILL_SECONDS` | hard timeout before the agent is killed |

The timer starts immediately before the agent process is launched and stops the
moment it returns. A run over `PASS_SECONDS` is a `time` failure but is allowed
to keep going up to `KILL_SECONDS`, at which point it is killed (still a `time`
failure). Override per run with `--pass-seconds` / `--kill-seconds`.

> Note: `--yolo` and the exact `--mcp` names are placeholders for your
> environment — set them to whatever your `agency copilot` actually uses.
