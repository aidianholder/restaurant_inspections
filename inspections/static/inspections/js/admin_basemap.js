/*
 * A vector basemap for the admin's map widget.
 *
 * Django's GIS widget ships with OpenStreetMap's public tile server, which
 * blocks sustained use — as it says it will. `MapWidget.layerBuilder` is the
 * documented extension point: register a named builder here and the widget
 * picks it by name from `base_layer`.
 *
 * OpenFreeMap serves vector tiles with no key and no usage limits, and its
 * style is self-contained: glyphs and sprites resolve from the same host, so
 * there is nothing else to host or keep alive.
 *
 * Loaded after gis/js/OLMapWidget.js — see ArkansasMapWidget.Media.
 */
(function () {
    "use strict";

    var STYLE_URL = window.INSPECTIONS_BASEMAP_STYLE ||
        "https://tiles.openfreemap.org/styles/liberty";

    // The style's own background layer belongs to no source, so applying the
    // style per-source skips it and land renders against whatever is behind the
    // canvas. This is liberty's background-color.
    var BACKGROUND = "#f8f4f0";

    if (typeof MapWidget === "undefined" || typeof olms === "undefined") {
        return;   // Django changed the widget, or the style library failed to load.
    }

    MapWidget.layerBuilder.openfreemap = function () {
        var layer = new ol.layer.VectorTile({
            declutter: true,
            // OpenStreetMap's licence requires attribution wherever its data is
            // shown, including an internal admin screen.
            source: undefined
        });

        olms.applyStyle(layer, STYLE_URL, "openmaptiles").then(function () {
            var source = layer.getSource();
            if (source && source.setAttributions) {
                source.setAttributions(
                    '<a href="https://openfreemap.org/" target="_blank" rel="noopener">OpenFreeMap</a> | ' +
                    '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>'
                );
            }
        }).catch(function (error) {
            // A blank basemap still leaves a usable widget: the point can be
            // placed from the coordinates, and the serialized field is visible.
            window.console && console.error("Basemap style failed to load:", error);
        });

        return layer;
    };

    // Paint the background before any tile arrives, so the widget never flashes
    // black on a slow connection.
    document.addEventListener("DOMContentLoaded", function () {
        document.querySelectorAll(".dj_map").forEach(function (el) {
            el.style.backgroundColor = BACKGROUND;
        });
    });
})();
