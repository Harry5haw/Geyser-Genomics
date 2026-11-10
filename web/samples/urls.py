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
    # The hosted site's upload page (static only, with a web API): straight to the data lake, token-gated.
    path("uploads/", views.upload_page, name="upload_page"),
    path("demo/", views.demo, name="demo"),
    # Debug only (404 otherwise; never published): a simulated run to watch the progress helix.
    path("demo/progress/", views.progress_demo, name="progress_demo"),
    path("demo/progress/frame/", views.progress_demo_frame, name="progress_demo_frame"),
    path("samples/<str:sample_id>/", views.sample_detail, name="detail"),
    # Calls pages as paths (not ?vpage=), so the static site can publish each as a folder with an index.html.
    path("samples/<str:sample_id>/calls/<int:page>/", views.sample_detail, name="detail_page"),
    path("samples/<str:sample_id>/status/", views.sample_status, name="status"),
    path("samples/<str:sample_id>/files/<str:kind>/", views.sample_file, name="file"),
]
