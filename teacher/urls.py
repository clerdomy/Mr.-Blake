from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("sw.js", views.service_worker, name="sw"),
    path("api/start", views.api_start, name="api_start"),
    path("api/assess", views.api_assess, name="api_assess"),
    path("api/speech", views.api_speech, name="api_speech"),
    path("api/health", views.api_health, name="api_health"),
]
