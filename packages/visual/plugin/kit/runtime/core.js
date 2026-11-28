/* Visual Kit core runtime: responsive SVG sizing (prevents <12px text scaling on narrow viewports). */
window.VK = window.VK || {};

window.VK.fitSvg = function fitSvg(svgOrSelector, height, drawFn) {
  var svg = typeof svgOrSelector === "string"
    ? document.querySelector(svgOrSelector)
    : svgOrSelector;
  if (!svg || typeof drawFn !== "function") return;

  function render() {
    var parent = svg.parentElement;
    var avail = svg.getBoundingClientRect().width;
    if ((!avail || avail < 40) && parent) {
      var cs = getComputedStyle(parent);
      avail = parent.clientWidth - parseFloat(cs.paddingLeft || "0") - parseFloat(cs.paddingRight || "0");
    }
    var w = Math.max(240, Math.floor(avail || 640));
    svg.setAttribute("viewBox", "0 0 " + w + " " + height);
    svg.setAttribute("width", String(w));
    svg.setAttribute("height", String(height));
    drawFn(svg, w, height);
  }

  render();
  if (typeof ResizeObserver === "function" && svg.parentElement) {
    new ResizeObserver(render).observe(svg.parentElement);
  }
};
