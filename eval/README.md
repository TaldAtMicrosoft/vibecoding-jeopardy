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
| `MCP_SERVERS` | servers passed as repeated `--mcp <name>` |
| `PASS_SECONDS` | the 2-minute (120s) pass threshold |
| `KILL_SECONDS` | hard timeout before the agent is killed (default 300s / 5 min) |

The timer starts immediately before the agent process is launched and stops the
moment it returns. A run over `PASS_SECONDS` is a `time` failure but is allowed
to keep going up to `KILL_SECONDS`, at which point it is killed (still a `time`
failure). Override per run with `--pass-seconds` / `--kill-seconds`.

> Note: `--yolo` and the exact `--mcp` names are placeholders for your
> environment — set them to whatever your `agency copilot` actually uses.
