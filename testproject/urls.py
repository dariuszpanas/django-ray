"""URL configuration for testproject."""

from django.contrib import admin
from django.urls import path

from testproject.api import api
from testproject.views import landing_page

urlpatterns = [
    path("", landing_page, name="landing-page"),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
