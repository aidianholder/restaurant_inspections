from django.urls import path

from . import embed_views, views

urlpatterns = [
    path("", views.facility_list, name="facility-list"),
    path("facility/<slug:slug>/", views.facility_detail, name="facility-detail"),
    path("output/", views.output_data, name="output-data"),
    path("output/summarize/", views.output_summary, name="output-summary"),
    path("output/render/", views.output_render, name="output-render"),
    path("retrieve/", views.scrape_request, name="scrape-request"),
    path("retrieve/<int:pk>/", views.scrape_detail, name="scrape-detail"),
    path("retrieve/<int:pk>/status/", views.scrape_status, name="scrape-status"),

    # Public embed endpoints. Framed by third parties by design.
    path("embed/<slug:slug>.js", embed_views.embed_loader, name="embed-loader"),
    path("embed/<slug:slug>/", embed_views.embed_page, name="embed-page"),
]
