(() => {
  for (const select of document.querySelectorAll(".auto-submit")) {
    select.addEventListener("change", () => select.form.requestSubmit());
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

  for (const details of document.querySelectorAll("[data-evidence-url]")) {
    details.addEventListener("toggle", async () => {
      if (!details.open || details.dataset.loaded === "true") return;
      const content = details.querySelector("[data-evidence-content]");
      if (!content) return;
      content.innerHTML = '<p class="small text-body-secondary mb-0">載入中…</p>';
      try {
        const response = await fetch(details.dataset.evidenceUrl, {
          headers: { Accept: "text/html" },
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        content.innerHTML = await response.text();
        details.dataset.loaded = "true";
      } catch (_error) {
        content.innerHTML =
          '<p class="small text-danger mb-0">日期推算證據暫時無法載入，請收合後重試。</p>';
      }
    });
  }
})();
