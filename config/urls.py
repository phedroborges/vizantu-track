from django.contrib import admin
from django.contrib.auth import views as auth
from django.urls import path
from crm import views
from crm import meta_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/', auth.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('logout/', auth.LogoutView.as_view(), name='logout'),
    path('health/', views.health),
    path('', views.home, name='home'),
    path('clients/new/', views.account_new, name='account_new'),
    path('a/<uuid:account_id>/', views.dashboard, name='dashboard'),
    path('a/<uuid:account_id>/leads/', views.leads, name='leads'),
    path('a/<uuid:account_id>/leads/<uuid:lead_id>/', views.lead_detail, name='lead_detail'),
    path('a/<uuid:account_id>/links/', views.links, name='links'),
    path('a/<uuid:account_id>/connections/', views.connections, name='connections'),
    path('a/<uuid:account_id>/settings/', views.account_settings, name='account_settings'),
    path('a/<uuid:account_id>/export/', views.export_csv, name='export_csv'),
    path('r/<uuid:link_id>/', views.track, name='track'),
    path('hooks/zapi/<uuid:connection_id>/<str:token>/', views.zapi_webhook, name='zapi_webhook'),
    path('hooks/omni/<uuid:connection_id>/<str:token>/', views.omni_webhook, name='omni_webhook'),
    path('a/<uuid:account_id>/meta/', meta_views.meta_settings, name='meta_settings'),
    path('a/<uuid:account_id>/meta/connect/', meta_views.meta_connect, name='meta_connect'),
    path('integrations/meta/callback/', meta_views.meta_callback, name='meta_callback'),
    path('a/<uuid:account_id>/ads/', meta_views.ads_report, name='ads_report'),
]
