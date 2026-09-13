"""Map widget for the admin.

Django's `OSMWidget` points at OpenStreetMap's public tile server, which blocks
sustained use — as its tile usage policy says it will. This swaps the basemap for
OpenFreeMap's vector tiles: no key, no usage limits, and a self-contained style
whose glyphs and sprites come from the same host, so there is nothing extra to
host or keep alive.

Only the basemap changes. Placing, dragging and clearing the point, and the
GeoJSON serialisation behind it, are all still Django's — reimplementing that to
change a tile source would be trading a small problem for a larger one.
"""

from django.contrib.gis.forms import OSMWidget

# Unpacked from Django's own widget so the OpenLayers version tracks whatever
# Django ships rather than being pinned here. If Django changes the shape of
# this tuple the unpack fails loudly at import, which is the point.
_OPENLAYERS_JS, _DJANGO_WIDGET_JS = OSMWidget.Media.js

# Applies a MapLibre/Mapbox style JSON to an OpenLayers layer. 12.2.1 is the last
# release whose peer range still covers the OpenLayers 7.2.2 Django bundles.
_OL_MAPBOX_STYLE_JS = "https://cdn.jsdelivr.net/npm/ol-mapbox-style@12.2.1/dist/olms.js"


class VectorBasemapWidget(OSMWidget):
    """The admin point-picker, on a basemap that won't rate-limit us."""

    base_layer = "openfreemap"

    # Centred on Arkansas rather than Django's default somewhere in Switzerland,
    # so a facility with no location yet opens somewhere useful.
    default_lon = -92.4
    default_lat = 34.8
    default_zoom = 7

    class Media:
        css = {
            "all": [
                *OSMWidget.Media.css["all"],
                "inspections/css/admin_basemap.css",
            ]
        }
        # Order matters: OpenLayers, then the style library, then Django's widget,
        # then our layer builder — which needs `MapWidget` and `olms` to exist.
        js = (
            _OPENLAYERS_JS,
            _OL_MAPBOX_STYLE_JS,
            _DJANGO_WIDGET_JS,
            "inspections/js/admin_basemap.js",
        )

    # The style URL lives in admin_basemap.js. Django's widget template renders
    # no extra context and `layerBuilder` callbacks take no arguments, so there
    # is nowhere for a setting to be threaded through without overriding the
    # template. Set `window.INSPECTIONS_BASEMAP_STYLE` before the script if a
    # deployment ever needs a different one; otherwise it is a one-line edit.
