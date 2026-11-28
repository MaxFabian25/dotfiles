/* Visual Kit stepper runtime: manages tablists with [data-vk-step] panels and arrow-key navigation. */
(function initSteppers() {
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-vk-stepper]").forEach(function (root) {
      var tabs = Array.from(root.querySelectorAll('[role="tab"][data-step]'));
      var panels = Array.from(root.querySelectorAll("[data-step-panel]"));
      if (!tabs.length) return;

      function activate(stepId, moveFocus) {
        tabs.forEach(function (t) {
          var on = t.dataset.step === stepId;
          t.setAttribute("aria-selected", String(on));
          t.tabIndex = on ? 0 : -1;
          if (on && moveFocus) t.focus();
        });
        panels.forEach(function (p) {
          p.hidden = p.dataset.stepPanel !== stepId;
        });
        root.dispatchEvent(new CustomEvent("vk:step", { detail: { step: stepId }, bubbles: true }));
      }

      tabs.forEach(function (tab, idx) {
        tab.addEventListener("click", function () { activate(tab.dataset.step, false); });
        tab.addEventListener("keydown", function (e) {
          if (e.key === "ArrowRight") {
            e.preventDefault();
            activate(tabs[(idx + 1) % tabs.length].dataset.step, true);
          } else if (e.key === "ArrowLeft") {
            e.preventDefault();
            activate(tabs[(idx - 1 + tabs.length) % tabs.length].dataset.step, true);
          }
        });
      });

      var initial = tabs.find(function (t) { return t.getAttribute("aria-selected") === "true"; }) || tabs[0];
      activate(initial.dataset.step, false);
    });
  });
})();
