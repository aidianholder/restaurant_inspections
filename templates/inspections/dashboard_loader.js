/*
 * Mount the "{{ dashboard.slug }}" inspections dashboard into this page.
 *
 *   <div data-arhi-dashboard></div>
 *   <script src=".../dashboard/{{ dashboard.slug }}/embed.js" async></script>
 *
 * With no such element, the component appears where the script tag sits.
 *
 * No iframe: WEHCO controls the papers' CSS and CSP, which removes both reasons
 * for one. Every class is prefixed `arhi-`, no ids are used, and MapLibre is
 * imported as an ES module — so nothing here can collide with the weather
 * features already running MapLibre on these pages.
 */
(function () {
    "use strict";

    var script = document.currentScript;
    var config = {{ config_json|safe }};
    var cssUrls = {{ css_urls_json|safe }};

    function addStyles() {
        cssUrls.forEach(function (href) {
            if (document.querySelector('link[href="' + href + '"]')) { return; }
            var link = document.createElement("link");
            link.rel = "stylesheet";
            link.href = href;
            document.head.appendChild(link);
        });
    }

    function host() {
        var selector = (script && script.getAttribute("data-target")) || "[data-arhi-dashboard]";
        var found = document.querySelector(selector);
        if (found) { return found; }
        var made = document.createElement("div");
        if (script && script.parentNode) { script.parentNode.insertBefore(made, script); }
        else { document.body.appendChild(made); }
        return made;
    }

    var target = host();
    addStyles();

    import("{{ module_url }}").then(function (module) {
        module.mountDashboard(target, config);
    }).catch(function (error) {
        if (window.console) { console.error("Inspections dashboard failed to load:", error); }
    });
})();
