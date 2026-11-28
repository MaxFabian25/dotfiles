/* Visual Kit PR diff renderer: import folding, whitespace collapse, moved-block detection. */
window.VK = window.VK || {};

(function initDiffRenderer() {
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function normWs(s) {
    return s.replace(/\s+/g, " ").trim();
  }

  function isImportLine(line) {
    var s = line.replace(/^[+ -]/, "").trim();
    return s.startsWith("import ") || s.startsWith("import{") || s.startsWith("} from ");
  }

  function isWsOnly(delLine, addLine) {
    return delLine.slice(1).replace(/\s/g, "") === addLine.slice(1).replace(/\s/g, "");
  }

  function detectMoves(dels, adds) {
    var TH = 3;
    var md = Object.create(null);
    var ma = Object.create(null);
    for (var di = 0; di < dels.length; di++) {
      if (md[di]) continue;
      var db = [di];
      for (var d2 = di + 1; d2 < dels.length && d2 - di < 40; d2++) {
        if (dels[d2].consecutive && !md[d2]) db.push(d2);
        else break;
      }
      if (db.length < TH) continue;
      var dn = db.map(function (i) { return normWs(dels[i].code); });
      for (var ai = 0; ai < adds.length; ai++) {
        if (ma[ai]) continue;
        var ab = [ai];
        for (var a2 = ai + 1; a2 < adds.length && a2 - ai < 40; a2++) {
          if (adds[a2].consecutive && !ma[a2]) ab.push(a2);
          else break;
        }
        if (ab.length < TH) continue;
        var an = ab.map(function (i) { return normWs(adds[i].code); });
        var ml = Math.min(dn.length, an.length);
        var mc = 0;
        for (var m = 0; m < ml; m++) {
          if (dn[m] === an[m]) mc++;
        }
        if (mc >= TH && mc >= ml * 0.7) {
          for (var k = 0; k < ml; k++) {
            md[db[k]] = true;
            ma[ab[k]] = true;
          }
          break;
        }
      }
    }
    return { movedDels: md, movedAdds: ma };
  }

  function renderDiff(target, diffInput, opts) {
    var el = typeof target === "string"
      ? (document.getElementById(target) || document.querySelector(target))
      : target;
    if (!el) return;
    var hideImports = !(opts && opts.showImports);
    var raw = Array.isArray(diffInput) ? diffInput : String(diffInput || "").split("\n");
    if (!raw.length || (raw.length === 1 && !raw[0])) {
      el.innerHTML = '<div style="padding:10px 12px;color:var(--vk-fg-muted);font-size:12px;">No diff data</div>';
      return;
    }

    var filtered = raw.filter(function (l) {
      if (l.startsWith("--- ") || l.startsWith("+++ ") || l.startsWith("@@") || l.startsWith("diff ")) return true;
      return hideImports ? !isImportLine(l) : true;
    });

    var collapsed = [];
    for (var i = 0; i < filtered.length; i++) {
      if (filtered[i].startsWith("-")) {
        var dr = [filtered[i]];
        var j = i + 1;
        while (j < filtered.length && filtered[j].startsWith("-")) { dr.push(filtered[j]); j++; }
        var ar = [];
        var k = j;
        while (k < filtered.length && filtered[k].startsWith("+")) { ar.push(filtered[k]); k++; }
        if (dr.length === ar.length && dr.length > 0 && dr.every(function (d, idx) { return isWsOnly(d, ar[idx]); })) {
          ar.forEach(function (a) { collapsed.push(" " + a.slice(1)); });
          i = k - 1;
          continue;
        }
      }
      collapsed.push(filtered[i]);
    }

    var dels = [];
    var adds = [];
    var parsed = [];
    var oL = 0;
    var nL = 0;
    var prevD = false;
    var prevA = false;

    collapsed.forEach(function (line) {
      if (line.startsWith("--- ") || line.startsWith("+++ ") || line.startsWith("diff ")) return;
      if (line.startsWith("@@")) {
        var m = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)/);
        if (m) { oL = parseInt(m[1], 10); nL = parseInt(m[2], 10); }
        parsed.push({ type: "hunk", text: line });
        prevD = false;
        prevA = false;
      } else if (line.startsWith("+")) {
        var ae = { type: "add", code: line.slice(1), newLine: nL, consecutive: prevA, idx: adds.length };
        adds.push(ae);
        parsed.push(ae);
        nL++;
        prevA = true;
        prevD = false;
      } else if (line.startsWith("-")) {
        var de = { type: "del", code: line.slice(1), oldLine: oL, consecutive: prevD, idx: dels.length };
        dels.push(de);
        parsed.push(de);
        oL++;
        prevD = true;
        prevA = false;
      } else {
        parsed.push({ type: "ctx", code: line.startsWith(" ") ? line.slice(1) : line, oldLine: oL, newLine: nL });
        oL++;
        nL++;
        prevD = false;
        prevA = false;
      }
    });

    var mv = detectMoves(dels, adds);
    var rows = parsed.map(function (p) {
      if (p.type === "hunk") {
        return '<tr class="vk-diff-hunk"><td class="vk-diff-ln"></td><td class="vk-diff-ln"></td><td>' + esc(p.text) + "</td></tr>";
      }
      if (p.type === "add") {
        var cls = mv.movedAdds[p.idx] ? "vk-diff-moved" : "vk-diff-add";
        var prefix = mv.movedAdds[p.idx] ? "~" : "+";
        return '<tr class="' + cls + '"><td class="vk-diff-ln"></td><td class="vk-diff-ln">' + p.newLine + "</td><td>" + prefix + esc(p.code) + "</td></tr>";
      }
      if (p.type === "del") {
        var cls2 = mv.movedDels[p.idx] ? "vk-diff-moved" : "vk-diff-del";
        var prefix2 = mv.movedDels[p.idx] ? "~" : "-";
        return '<tr class="' + cls2 + '"><td class="vk-diff-ln">' + p.oldLine + '</td><td class="vk-diff-ln"></td><td>' + prefix2 + esc(p.code) + "</td></tr>";
      }
      return '<tr><td class="vk-diff-ln">' + p.oldLine + '</td><td class="vk-diff-ln">' + p.newLine + "</td><td> " + esc(p.code) + "</td></tr>";
    });

    el.innerHTML = '<div class="vk-diff-scroll"><table class="vk-diff-table"><tbody>' + rows.join("") + "</tbody></table></div>";
  }

  window.VK.renderDiff = renderDiff;

  document.addEventListener("DOMContentLoaded", function () {
    var payloadEl = document.getElementById("vk-diffs-json");
    if (!payloadEl) return;
    var diffs;
    try {
      diffs = JSON.parse(payloadEl.textContent || "{}");
    } catch (err) {
      console.error("Visual Kit: failed to parse vk-diffs-json", err);
      return;
    }
    document.querySelectorAll("[data-vk-diff]").forEach(function (node) {
      var key = node.getAttribute("data-vk-diff");
      if (key && Object.prototype.hasOwnProperty.call(diffs, key)) {
        renderDiff(node, diffs[key], { showImports: node.hasAttribute("data-show-imports") });
      }
    });
  });
})();
