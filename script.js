const CSV_PATH = "data/cards.csv";
const STAGES = ["points", "question", "answer"];
const HIDDEN_POINT_VALUE = 600;
// Categories present in the data but intentionally not playable in the UI yet.
const DISABLED_CATEGORIES = new Set(["Build Me!"]);

const toPoints = (value) => parseInt(String(value).replace(/[^0-9]/g, ""), 10);
const isPlaceholderCard = (card) => !card.question || card.question.trim() === "";
const isDisabledCard = (card) => DISABLED_CATEGORIES.has(card.category) || isPlaceholderCard(card);

const board = document.querySelector("#board");
const statusMessage = document.querySelector("#status");
const resetButton = document.querySelector("#reset-board");
const cardTemplate = document.querySelector("#card-template");

const cardModal = document.querySelector("#card-modal");
const cardModalSlot = cardModal.querySelector(".card-modal__slot");
let maximizedCard = null;
let modalPlaceholder = null;

function openCardModal(cardButton) {
  if (maximizedCard) {
    return;
  }
  modalPlaceholder = document.createComment("maximized-card");
  cardButton.replaceWith(modalPlaceholder);
  cardModalSlot.append(cardButton);
  cardButton.classList.add("is-maximized");
  maximizedCard = cardButton;
  cardModal.hidden = false;
  cardModal.setAttribute("aria-hidden", "false");
  document.body.classList.add("is-modal-open");
  cardButton.focus();
}

function closeCardModal() {
  if (!maximizedCard) {
    return;
  }
  const cardButton = maximizedCard;
  cardButton.classList.remove("is-maximized");
  modalPlaceholder.replaceWith(cardButton);
  modalPlaceholder = null;
  maximizedCard = null;
  cardModal.hidden = true;
  cardModal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("is-modal-open");
  cardButton.focus();
}

cardModal.addEventListener("click", (event) => {
  if (event.target.closest("[data-modal-close]")) {
    closeCardModal();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && maximizedCard) {
    closeCardModal();
  }
});

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
  const copyButton = cardButton.querySelector(".card-copy");
  const maximizeButton = cardButton.querySelector(".card-maximize");
  const closeButton = cardButton.querySelector(".card-close");
  let stageIndex = 0;

  function renderCard() {
    const stageName = STAGES[stageIndex];
    cardButton.dataset.stage = String(stageIndex);
    cardButton.classList.toggle("is-revealed", stageIndex > 0);
    cardButton.classList.toggle("is-complete", stageIndex === STAGES.length - 1);

    const atAnswer = stageName === "answer";
    copyButton.hidden = !atAnswer;

    if (stageName === "points") {
      label.textContent = `${card.points}`;
      stage.textContent = "Click to reveal";
    } else {
      label.textContent = card[stageName];
      stage.textContent = atAnswer ? "Known-good prompt" : stageName;
    }
  }

  function advance() {
    const previousStage = stageIndex;
    stageIndex = Math.min(stageIndex + 1, STAGES.length - 1);
    if (stageIndex !== previousStage) {
      cardButton.classList.remove("is-flashing");
      void cardButton.offsetWidth;
      cardButton.classList.add("is-flashing");
    }
    renderCard();
  }

  cardButton.addEventListener("click", (event) => {
    if (event.target.closest(".card-copy") || event.target.closest(".card-controls")) {
      return;
    }
    advance();
  });

  cardButton.addEventListener("keydown", (event) => {
    if (event.target.closest(".card-copy") || event.target.closest(".card-controls")) {
      return;
    }
    if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
      event.preventDefault();
      advance();
    }
  });

  maximizeButton.addEventListener("click", (event) => {
    event.stopPropagation();
    openCardModal(cardButton);
  });

  closeButton.addEventListener("click", (event) => {
    event.stopPropagation();
    if (cardButton.classList.contains("is-maximized")) {
      closeCardModal();
    } else {
      cardButton.reset();
    }
  });

  copyButton.addEventListener("click", async (event) => {
    event.stopPropagation();
    const original = "Copy prompt";
    try {
      await navigator.clipboard.writeText(card.answer);
      copyButton.textContent = "Copied!";
    } catch (error) {
      const range = document.createRange();
      range.selectNodeContents(label);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      copyButton.textContent = "Selected — press Ctrl+C";
    }
    window.setTimeout(() => {
      copyButton.textContent = original;
    }, 1600);
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

function createHiddenCard(card) {  const cardElement = document.createElement("div");
  cardElement.className = "card is-injection";
  cardElement.dataset.category = card.category;
  cardElement.dataset.points = card.points;

  const fields = [
    ["points", card.points],
    ["question", card.question],
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

function createComingSoonCard(card) {
  const cardElement = document.createElement("div");
  cardElement.className = "card is-coming-soon";
  cardElement.dataset.stage = "0";
  cardElement.setAttribute("aria-disabled", "true");

  const label = document.createElement("span");
  label.className = "card-label";
  label.textContent = `${toPoints(card.points)}`;

  const stage = document.createElement("span");
  stage.className = "card-stage";
  stage.textContent = "Coming soon";

  cardElement.append(label, stage);
  return cardElement;
}

function renderBoard() {
  const groupedCards = groupCardsByCategory(cards);
  const categories = [...groupedCards.keys()];
  const pointValues = [
    ...new Set(cards.map((card) => toPoints(card.points)).filter((value) => Number.isFinite(value))),
  ].sort((a, b) => a - b);
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
      const card = groupedCards.get(category).find((item) => toPoints(item.points) === points);
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

      if (isDisabledCard(card)) {
        row.append(createComingSoonCard(card));
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

  const visibleCards = cards.filter(
    (card) => toPoints(card.points) !== HIDDEN_POINT_VALUE && !isDisabledCard(card)
  );
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

async function loadTips() {
  const tipsCard = document.querySelector("#tips-card");
  if (!tipsCard) {
    return;
  }
  try {
    const response = await fetch("data/tips.csv");
    if (!response.ok) {
      return;
    }
    const tips = parseCsv(await response.text()).filter(
      (tip) => (tip.content || "").trim() !== ""
    );
    if (tips.length === 0) {
      return;
    }

    const contentEl = tipsCard.querySelector(".tips-card__content");
    const footerEl = tipsCard.querySelector(".tips-card__footer");
    const nextButton = tipsCard.querySelector(".tips-card__next");
    const prevButton = tipsCard.querySelector(".tips-card__prev");
    const maximizeButton = tipsCard.querySelector(".card-maximize");
    const closeButton = tipsCard.querySelector(".card-close");
    let index = 0;

    const showTip = () => {
      const tip = tips[index];
      contentEl.textContent = tip.content;
      footerEl.textContent = `${tip.category} - ${tip.points}`;
    };

    if (tips.length <= 1) {
      nextButton.hidden = true;
      prevButton.hidden = true;
    } else {
      nextButton.addEventListener("click", () => {
        index = (index + 1) % tips.length;
        showTip();
      });
      prevButton.addEventListener("click", () => {
        index = (index - 1 + tips.length) % tips.length;
        showTip();
      });
    }

    maximizeButton.addEventListener("click", (event) => {
      event.stopPropagation();
      openCardModal(tipsCard);
    });

    closeButton.addEventListener("click", (event) => {
      event.stopPropagation();
      if (tipsCard.classList.contains("is-maximized")) {
        closeCardModal();
      }
    });

    showTip();
    tipsCard.hidden = false;
  } catch (error) {
    /* Tips are a non-critical enhancement — fail quietly. */
  }
}

loadCards();
loadTips();
