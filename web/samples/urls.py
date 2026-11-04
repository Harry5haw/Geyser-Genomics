from django.urls import path

from . import views

app_name = "samples"

urlpatterns = [
    path("", views.sample_list, name="list"),
    path("samples/rows/", views.sample_rows, name="rows"),
    path("figures/", views.figures_page, name="figures"),
    path("figures/<str:name>.json", views.figure_data, name="figure_data"),
    path("figures/<str:name>.csv", views.figure_csv, name="figure_csv"),
    path("upload/", views.upload, name="upload"),
    path("demo/", views.demo, name="demo"),
    path("samples/<str:sample_id>/", views.sample_detail, name="detail"),
    path("samples/<str:sample_id>/status/", views.sample_status, name="status"),
    path("samples/<str:sample_id>/files/<str:kind>/", views.sample_file, name="file"),
]
