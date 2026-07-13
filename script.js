const CSV_PATH = "data/cards.csv";
const STAGES = ["points", "question", "hint", "answer"];
const HIDDEN_POINT_VALUE = 600;

const board = document.querySelector("#board");
const statusMessage = document.querySelector("#status");
const resetButton = document.querySelector("#reset-board");
const cardTemplate = document.querySelector("#card-template");

let cards = [];

function parseCsv(text) {
  const rows = [];
  let row = [];
  let value = "";
  let inQuotes = false;

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const nextChar = text[index + 1];

    if (char === '"' && inQuotes && nextChar === '"') {
      value += '"';
      index += 1;
    } else if (char === '"') {
      inQuotes = !inQuotes;
    } else if (char === "," && !inQuotes) {
      row.push(value);
      value = "";
    } else if ((char === "\n" || char === "\r") && !inQuotes) {
      if (char === "\r" && nextChar === "\n") {
        index += 1;
      }
      row.push(value);
      if (row.some((cell) => cell.trim() !== "")) {
        rows.push(row);
      }
      row = [];
      value = "";
    } else {
      value += char;
    }
  }

  row.push(value);
  if (row.some((cell) => cell.trim() !== "")) {
    rows.push(row);
  }

  const [headers, ...dataRows] = rows;
  return dataRows.map((dataRow) =>
    Object.fromEntries(headers.map((header, index) => [header.trim(), (dataRow[index] || "").trim()]))
  );
}

function groupCardsByCategory(allCards) {
  return allCards.reduce((groups, card) => {
    if (!groups.has(card.category)) {
      groups.set(card.category, []);
    }
    groups.get(card.category).push(card);
    return groups;
  }, new Map());
}

function createCard(card) {
  const cardButton = cardTemplate.content.firstElementChild.cloneNode(true);
  const label = cardButton.querySelector(".card-label");
  const stage = cardButton.querySelector(".card-stage");
  let stageIndex = 0;

  function renderCard() {
    const stageName = STAGES[stageIndex];
    cardButton.dataset.stage = String(stageIndex);
    cardButton.classList.toggle("is-revealed", stageIndex > 0);
    cardButton.classList.toggle("is-complete", stageIndex === STAGES.length - 1);

    if (stageName === "points") {
      label.textContent = `${card.points}`;
      stage.textContent = "Click to reveal";
    } else {
      label.textContent = card[stageName];
      stage.textContent = stageName === "answer" ? "Known-good prompt" : stageName;
    }
  }

  cardButton.addEventListener("click", () => {
    const previousStage = stageIndex;
    stageIndex = Math.min(stageIndex + 1, STAGES.length - 1);
    if (stageIndex !== previousStage) {
      cardButton.classList.remove("is-flashing");
      void cardButton.offsetWidth;
      cardButton.classList.add("is-flashing");
    }
    renderCard();
  });

  cardButton.addEventListener("animationend", (event) => {
    if (event.animationName === "tv-static") {
      cardButton.classList.remove("is-flashing");
    }
  });

  cardButton.reset = () => {
    stageIndex = 0;
    renderCard();
  };

  renderCard();
  return cardButton;
}

function createHiddenCard(card) {
  const cardElement = document.createElement("div");
  cardElement.className = "card is-injection";
  cardElement.dataset.category = card.category;
  cardElement.dataset.points = card.points;

  const fields = [
    ["points", card.points],
    ["question", card.question],
    ["hint", card.hint],
    ["answer", card.answer],
  ];

  fields.forEach(([name, text]) => {
    const span = document.createElement("span");
    span.className = `injection-field injection-${name}`;
    span.dataset.field = name;
    span.textContent = text;
    cardElement.append(span);
  });

  return cardElement;
}

function renderBoard() {
  const groupedCards = groupCardsByCategory(cards);
  const categories = [...groupedCards.keys()];
  const pointValues = [...new Set(cards.map((card) => Number(card.points)))].sort((a, b) => a - b);
  const cardElements = [];

  board.innerHTML = "";
  board.style.setProperty("--category-count", categories.length);

  const categoryRow = document.createElement("div");
  categoryRow.className = "category-row";

  categories.forEach((category) => {
    const categoryHeading = document.createElement("div");
    categoryHeading.className = "category";
    categoryHeading.textContent = category;
    categoryRow.append(categoryHeading);
  });

  board.append(categoryRow);

  pointValues.forEach((points) => {
    const row = document.createElement("div");
    row.className = "card-row";
    row.classList.toggle("is-hidden-injection", points === HIDDEN_POINT_VALUE);

    categories.forEach((category) => {
      const card = groupedCards.get(category).find((item) => Number(item.points) === points);
      if (!card) {
        const spacer = document.createElement("div");
        spacer.className = "card";
        spacer.textContent = "No card";
        row.append(spacer);
        return;
      }

      if (points === HIDDEN_POINT_VALUE) {
        row.append(createHiddenCard(card));
        return;
      }

      const cardElement = createCard(card);
      cardElements.push(cardElement);
      row.append(cardElement);
    });

    board.append(row);
  });

  resetButton.onclick = () => {
    cardElements.forEach((cardElement) => cardElement.reset());
  };

  const visibleCards = cards.filter((card) => Number(card.points) !== HIDDEN_POINT_VALUE);
  statusMessage.textContent = `${visibleCards.length} cards loaded from ${CSV_PATH}`;
}

async function loadCards() {
  try {
    const response = await fetch(CSV_PATH);
    if (!response.ok) {
      throw new Error(`Could not load ${CSV_PATH}`);
    }

    cards = parseCsv(await response.text());
    renderBoard();
  } catch (error) {
    statusMessage.textContent = error.message;
  }
}

loadCards();
