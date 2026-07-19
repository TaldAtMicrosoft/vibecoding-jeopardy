# Playwright UI tests

Browser tests for the Vibecoding Jeopardy static site, driven by **Playwright for
.NET**. Used instead of npm/npx Playwright because the npm registry is blocked in
this environment; the `Microsoft.Playwright` package is restored from the
`CELADemosAndPOCs` Azure Artifacts feed (see `nuget.config`).

## What it checks

`Program.cs` is a self-contained console harness that:

1. Serves the repo root over `http://localhost:8153/` (Playwright can't `fetch`
   `data/*.csv` over `file://`).
2. Launches headless Chromium and loads the board.
3. Verifies the **Tips card**:
   - first tip shown is the lowest `index` in `data/tips.csv`,
   - **Next** steps through every tip in `index` order and wraps to the first,
   - **Back** from the first tip wraps to the last,
   - the Back button is visible and not greyed out (opacity 1).

It exits non-zero if any check fails.

## Run

```powershell
cd tests/playwright
dotnet build -c Release
# one-time browser install (downloads from Playwright's CDN, not npm):
pwsh bin/Release/net10.0/playwright.ps1 install chromium
dotnet run -c Release --no-build
```

`bin/` and `obj/` are git-ignored.
