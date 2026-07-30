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

})();
