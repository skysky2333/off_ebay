from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Off-Ebay Administration"
admin.site.site_title = "Off-Ebay"
admin.site.index_title = "Operations"

handler500 = "storefront.views.server_error"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("storefront.urls")),
]
