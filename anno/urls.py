from django.conf.urls import url

from . import views

urlpatterns = [
    # these are for back-compat
    url(r'^search', views.search_back_compat_api, name='compat_search'),
    url(r'^create$', views.crud_compat_create, name='compat_create'),
    url(r'^update/(?P<anno_id>[0-9a-zA-z-]+)$',
        views.crud_compat_update, name='compat_update'),
    url(r'^delete/(?P<anno_id>[0-9a-zA-z-]+)$',
        views.crud_compat_delete, name='compat_delete'),

    # these are for catchpy v2
    url(r'^$', views.crud_create, name='crud_create'),
    url(r'^\?', views.search_api, name='search_api_clear'),
    url(r'^(?P<anno_id>[0-9a-zA-z-]+)$', views.crud_api, name='crud_api'),
]
