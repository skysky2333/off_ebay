from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "FM2K Store Administration"
admin.site.site_title = "FM2K Store"
admin.site.index_title = "Operations"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("storefront.urls")),
]
