/* Issue #561: initialize the VENDORED mermaid build (assets/js/mermaid.min.js,
 * mermaid@11.4.1 UMD dist -- no CDN, so the built site renders diagrams fully
 * offline). pymdownx.superfences' fence_code_format emits
 * <pre class="mermaid"><code>...</code></pre>; mermaid expects the diagram
 * source as the element's direct text, so unwrap the <code> first, then run.
 * Theme variables approximate the Dracula palette used by assets/dracula.css. */
(function () {
  function render() {
    if (typeof mermaid === "undefined") return;
    document.querySelectorAll("pre.mermaid > code").forEach(function (code) {
      var pre = code.parentElement;
      var div = document.createElement("div");
      div.className = "mermaid";
      div.textContent = code.textContent;
      pre.replaceWith(div);
    });
    mermaid.initialize({
      startOnLoad: false,
      theme: "dark",
      themeVariables: {
        background: "#282a36",
        primaryColor: "#44475a",
        primaryTextColor: "#f8f8f2",
        primaryBorderColor: "#bd93f9",
        lineColor: "#6272a4",
        secondaryColor: "#343746",
        tertiaryColor: "#282a36",
        noteBkgColor: "#44475a",
        noteTextColor: "#f8f8f2",
        actorTextColor: "#f8f8f2",
        signalTextColor: "#f8f8f2",
      },
    });
    mermaid.run({ querySelector: "div.mermaid" });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
