from django.urls import path

from . import views

urlpatterns = [
    path("", views.facility_list, name="facility-list"),
    path("facility/<slug:slug>/", views.facility_detail, name="facility-detail"),
    path("retrieve/", views.scrape_request, name="scrape-request"),
    path("retrieve/<int:pk>/", views.scrape_detail, name="scrape-detail"),
    path("retrieve/<int:pk>/status/", views.scrape_status, name="scrape-status"),
]
