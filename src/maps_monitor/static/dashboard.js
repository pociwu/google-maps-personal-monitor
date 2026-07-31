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
