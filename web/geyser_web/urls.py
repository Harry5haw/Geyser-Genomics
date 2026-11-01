from django.urls import include, path

urlpatterns = [
    path("", include("samples.urls")),
]
