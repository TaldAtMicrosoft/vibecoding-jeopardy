# Vibecoding 1:01 Jeopardy

A simple GitHub Pages-ready Jeopardy-style teaching site. Cards are loaded from `data/cards.csv`, so the CSV acts as the lightweight backend for the board.

## Files

- `index.html` - page shell
- `styles.css` - visual design
- `script.js` - CSV loading and card interactions
- `data/cards.csv` - categories, point values, prompts, hints, and known-good prompts

## Run locally

Because the page loads a CSV file, use a local web server instead of opening `index.html` directly:

```powershell
python -m http.server 8080
```

Then open `http://localhost:8080`.

## Publish with GitHub Pages

1. Push this folder to a GitHub repository.
2. In the repository, go to **Settings > Pages**.
3. Choose the branch and folder that contain `index.html`.
4. Save, then open the generated GitHub Pages URL.

## Editing the game

Update `data/cards.csv`. Each row is one card:

```csv
category,points,question,hint,answer
Work IQ,100,Use Work IQ to find the subject of your last received email.,If you were standing behind someone else how would you ask them to search this information?,Find the subject of my most recently received email.
```

Use five categories and five point values per category for the intended board shape. The current sample categories are:

- `Data Sources`
- `Break It Down`
- `Visualize It`
- `Make It Work`
- `Build It`

The `Build It` column is designed so each prompt can be run independently or out of order, similar to assigning parallel tasks to multiple coding agents.

## Lab safety canary

The CSV also includes hidden `600`-point rows that are rendered into the DOM but hidden from the visible board. These rows are tagged with `<prompt-injection>` and `</prompt-injection>` so coding agents and users can identify them as part of the presentation.

Before using the GitHub issue telemetry checkpoint, replace `OWNER/REPO` in `data/cards.csv` with the public repository that should receive lab canary issues.
