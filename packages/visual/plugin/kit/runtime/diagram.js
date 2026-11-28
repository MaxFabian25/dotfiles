/* Visual Kit diagram runtime: binds clickable [data-vk-node] buttons to a shared native popover inspector. */
(function initDiagramInspectors() {
  document.addEventListener("DOMContentLoaded", function () {
    var pop = document.getElementById("vk-inspector");
    if (!pop) return;
    var titleEl = pop.querySelector("[data-vk-pop-title]");
    var bodyEl = pop.querySelector("[data-vk-pop-body]");
    var codeEl = pop.querySelector("[data-vk-pop-path]");

    document.querySelectorAll("[data-vk-node]").forEach(function (btn) {
      if (!btn.hasAttribute("popovertarget")) {
        btn.setAttribute("popovertarget", "vk-inspector");
      }
      btn.addEventListener("click", function () {
        if (titleEl) titleEl.textContent = btn.dataset.title || btn.textContent.trim();
        if (bodyEl) bodyEl.textContent = btn.dataset.desc || "";
        if (codeEl) {
          codeEl.textContent = btn.dataset.path || "";
          codeEl.hidden = !btn.dataset.path;
        }
      });
    });
  });
})();
