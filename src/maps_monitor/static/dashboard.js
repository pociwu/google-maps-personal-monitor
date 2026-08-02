(() => {
  for (const select of document.querySelectorAll(".auto-submit")) {
    select.addEventListener("change", () => select.form.requestSubmit());
  }

  for (const button of document.querySelectorAll(".remove-target")) {
    button.addEventListener("click", (event) => {
      const name = button.dataset.targetName || "這位貢獻者";
      if (!window.confirm(`確定停止監控「${name}」？歷史資料仍會保留。`)) {
        event.preventDefault();
      }
    });
  }

  const contributorStrip = document.getElementById("contributor-strip");
  const reorderStatus = document.getElementById("reorder-status");
  let draggingCard = null;
  let orderBeforeDrag = "";
  let pointerCard = null;
  let pointerStartX = 0;
  let pointerStartY = 0;
  let pointerMoved = false;
  let saveQueue = Promise.resolve();

  const reorderableCards = () => [
    ...document.querySelectorAll(".reorderable-contributor[data-target-url]"),
  ];
  const serializedOrder = () => reorderableCards()
    .map((card) => card.dataset.targetUrl)
    .join("\n");

  const saveContributorOrder = () => {
    if (!contributorStrip) return;
    const targetUrls = reorderableCards().map((card) => card.dataset.targetUrl);
    saveQueue = saveQueue.then(async () => {
      if (reorderStatus) reorderStatus.textContent = "正在儲存順序…";
      try {
        const response = await fetch("/targets/reorder", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({target_urls: targetUrls}),
        });
        if (!response.ok) throw new Error("reorder failed");
        if (reorderStatus) reorderStatus.textContent = "順序已儲存。";
      } catch (_error) {
        if (reorderStatus) reorderStatus.textContent = "順序儲存失敗，正在還原頁面。";
        window.setTimeout(() => window.location.reload(), 800);
      }
    });
    return saveQueue;
  };

  if (contributorStrip) {
    for (const card of reorderableCards()) {
      const handle = card.querySelector(".drag-handle");
      if (!handle) continue;

      handle.addEventListener("pointerdown", (event) => {
        if (event.pointerType === "mouse") {
          card.draggable = true;
          return;
        }
        pointerCard = card;
        pointerStartX = event.clientX;
        pointerStartY = event.clientY;
        pointerMoved = false;
        orderBeforeDrag = serializedOrder();
        handle.setPointerCapture(event.pointerId);
      });
      handle.addEventListener("pointermove", (event) => {
        if (pointerCard !== card) return;
        const distance = Math.hypot(
          event.clientX - pointerStartX,
          event.clientY - pointerStartY,
        );
        if (!pointerMoved && distance < 8) return;
        pointerMoved = true;
        card.classList.add("dragging");
        event.preventDefault();
        const target = document.elementFromPoint(event.clientX, event.clientY)
          ?.closest(".reorderable-contributor[data-target-url]");
        if (!target || target === card) return;
        const bounds = target.getBoundingClientRect();
        if (event.clientX < bounds.left + bounds.width / 2) target.before(card);
        else target.after(card);
      });
      handle.addEventListener("pointerup", async () => {
        if (draggingCard !== card) card.draggable = false;
        if (pointerCard !== card) return;
        pointerCard = null;
        card.classList.remove("dragging");
        if (pointerMoved && serializedOrder() !== orderBeforeDrag) {
          await saveContributorOrder();
        }
      });
      handle.addEventListener("pointercancel", () => {
        if (pointerCard !== card) return;
        pointerCard = null;
        card.classList.remove("dragging");
        window.location.reload();
      });
      handle.addEventListener("keydown", async (event) => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        const cards = reorderableCards();
        const index = cards.indexOf(card);
        if (event.key === 'ArrowLeft' && index > 0) {
          cards[index - 1].before(card);
        } else if (event.key === 'ArrowRight' && index < cards.length - 1) {
          cards[index + 1].after(card);
        } else {
          return;
        }
        await saveContributorOrder();
        handle.focus();
      });
      card.addEventListener("dragstart", (event) => {
        if (!card.draggable) {
          event.preventDefault();
          return;
        }
        draggingCard = card;
        orderBeforeDrag = serializedOrder();
        card.classList.add("dragging");
        if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
      });
      card.addEventListener("dragend", async () => {
        card.classList.remove("dragging");
        card.draggable = false;
        draggingCard = null;
        if (serializedOrder() !== orderBeforeDrag) await saveContributorOrder();
      });
    }

    contributorStrip.addEventListener("dragover", (event) => {
      if (!draggingCard) return;
      event.preventDefault();
      const candidates = reorderableCards().filter((card) => card !== draggingCard);
      const after = candidates.find((card) => {
        const bounds = card.getBoundingClientRect();
        return event.clientX < bounds.left + bounds.width / 2;
      });
      if (after) contributorStrip.insertBefore(draggingCard, after);
      else contributorStrip.appendChild(draggingCard);
    });
  }

  const modal = document.getElementById("imageModal");
  const image = document.getElementById("modalImage");
  if (modal && image) {
    modal.addEventListener("show.bs.modal", (event) => {
      const trigger = event.relatedTarget;
      image.src = trigger?.dataset.original || "";
    });
    modal.addEventListener("hidden.bs.modal", () => {
      image.removeAttribute("src");
    });
  }

})();
