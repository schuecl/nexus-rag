/* Click-to-enlarge for content images and mermaid diagrams.
 * Self-contained (no CDN, no plugin dependency) to keep the built site fully
 * offline-capable, matching the vendored-mermaid posture. Targets:
 *   - content images (charts) — excluding inline emoji (.twemoji)
 *   - rendered mermaid diagrams (div.mermaid > svg)
 * Opens a fullscreen dark overlay; click anywhere or press Escape to close. */
(function () {
  function makeOverlay(contentNode) {
    var overlay = document.createElement("div");
    overlay.className = "nx-lightbox";
    overlay.appendChild(contentNode);
    function close() {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
    }
    function onKey(e) {
      if (e.key === "Escape") close();
    }
    overlay.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    document.body.appendChild(overlay);
  }

  function bindImages() {
    document
      .querySelectorAll(".md-typeset img:not(.twemoji):not([class*='emoji'])")
      .forEach(function (img) {
        if (img.dataset.nxLightbox) return;
        img.dataset.nxLightbox = "1";
        img.classList.add("nx-zoomable");
        img.addEventListener("click", function () {
          var big = document.createElement("img");
          big.src = img.currentSrc || img.src;
          big.alt = img.alt || "";
          makeOverlay(big);
        });
      });
  }

  function bindMermaid() {
    document.querySelectorAll("div.mermaid").forEach(function (holder) {
      var svg = holder.querySelector("svg");
      if (!svg || holder.dataset.nxLightbox) return;
      holder.dataset.nxLightbox = "1";
      holder.classList.add("nx-zoomable");
      function closeIfOpen(e) {
        if (holder.classList.contains("nx-open") && (!e || e.key === "Escape")) {
          holder.classList.remove("nx-open");
        }
      }
      holder.addEventListener("click", function () {
        // toggle the ORIGINAL node fullscreen -- cloning a mermaid svg breaks
        // its scoped styles; repositioning keeps everything attached.
        holder.classList.toggle("nx-open");
      });
      document.addEventListener("keydown", closeIfOpen);
    });
  }

  function init() {
    bindImages();
    bindMermaid();
    // mermaid renders asynchronously after mermaid-init.js runs — retry
    // briefly until its SVGs exist, then bind them too.
    var tries = 0;
    var timer = setInterval(function () {
      bindMermaid();
      if (++tries > 20) clearInterval(timer);
    }, 400);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
