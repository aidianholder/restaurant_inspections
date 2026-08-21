/* {{ embed.heading }} — embed loader.
   Paste one line into the page; this injects the iframe and keeps its height in
   step with the content as readers expand rows, search and page through. */
(function () {
  "use strict";
  var SLUG = "{{ embed.slug|escapejs }}";
  var SRC = "{{ embed_url|escapejs }}";
  var ORIGIN = SRC.split("/").slice(0, 3).join("/");

  // `document.currentScript` is correct for classic scripts, async included; the
  // fallback covers a CMS that rewrites or relocates the tag.
  var script = document.currentScript;
  if (!script) {
    var all = document.getElementsByTagName("script");
    for (var i = all.length - 1; i >= 0; i--) {
      if (all[i].src && all[i].src.indexOf("{{ loader_path|escapejs }}") !== -1) { script = all[i]; break; }
    }
  }

  var frame = document.createElement("iframe");
  frame.src = SRC;
  frame.title = "{{ embed.heading|escapejs }}";
  frame.loading = "lazy";
  frame.setAttribute("scrolling", "no");
  frame.style.cssText = "width:100%;border:0;display:block;overflow:hidden;min-height:220px";
  // A sane starting height so the page doesn't visibly jump before the first
  // measurement arrives.
  frame.height = "600";

  if (script && script.parentNode) {
    script.parentNode.insertBefore(frame, script.nextSibling);
  } else {
    document.body.appendChild(frame);
  }

  window.addEventListener("message", function (event) {
    if (event.origin !== ORIGIN) return;             // only our own iframe
    var data = event.data;
    if (!data || data.embed !== SLUG || data.type !== "height") return;
    var height = parseInt(data.height, 10);
    if (height >= 0) { frame.style.height = height + "px"; }
  });
})();
