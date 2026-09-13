from django.urls import path

from . import dashboard_views, embed_views, views

urlpatterns = [
    path("", views.facility_list, name="facility-list"),
    path("facility/<slug:slug>/", views.facility_detail, name="facility-detail"),
    path("output/", views.output_data, name="output-data"),
    path("output/summarize/", views.output_summary, name="output-summary"),
    path("output/render/", views.output_render, name="output-render"),
    path("retrieve/", views.scrape_request, name="scrape-request"),
    path("retrieve/<int:pk>/", views.scrape_detail, name="scrape-detail"),
    path("retrieve/<int:pk>/status/", views.scrape_status, name="scrape-status"),

    # Reader-facing establishment page, linked from the dashboard. Public, like
    # everything under /dashboard/ and /embed/ — unlike the staff views above.
    path("establishment/<slug:slug>/", dashboard_views.establishment, name="establishment"),

    # Per-newspaper dashboard: a JSON API, a canonical page, and a loader that
    # mounts the same component into a paper's own template.
    path("dashboard/<slug:slug>/", dashboard_views.page, name="dashboard-page"),
    path("dashboard/<slug:slug>/embed.js", dashboard_views.loader, name="dashboard-loader"),
    path("dashboard/<slug:slug>/api/rows", dashboard_views.rows, name="dashboard-rows"),
    path("dashboard/<slug:slug>/api/map", dashboard_views.map_data, name="dashboard-map"),
    path("dashboard/<slug:slug>/api/facility/<int:facility_id>",
         dashboard_views.facility_row, name="dashboard-facility"),

    # Public embed endpoints. Framed by third parties by design.
    path("embed/<slug:slug>.js", embed_views.embed_loader, name="embed-loader"),
    path("embed/<slug:slug>/", embed_views.embed_page, name="embed-page"),
]
