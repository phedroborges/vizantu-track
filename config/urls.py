from django.contrib import admin
from django.contrib.auth import views as auth
from django.urls import path
from crm import views
from crm import meta_views
from crm import whatsapp_views
from crm import funnel_views
from crm import portal_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/', auth.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('logout/', auth.LogoutView.as_view(), name='logout'),
    path('health/', views.health),
    path('', portal_views.agency, name='home'),
    path('clients/', views.home, name='clients'),
    path('dashboards/', portal_views.dashboards, name='agency_dashboards'),
    path('a/<uuid:account_id>/dashboards/', portal_views.dashboards, name='client_dashboards'),
    path('a/<uuid:account_id>/reports/<slug:section>/', portal_views.channel, name='channel_report'),
    path('a/<uuid:account_id>/sales/', portal_views.sales, name='sales'),
    path('a/<uuid:account_id>/integrations/', portal_views.integrations, name='integrations'),
    path('clients/new/', views.account_new, name='account_new'),
    path('a/<uuid:account_id>/', views.dashboard, name='dashboard'),
    path('a/<uuid:account_id>/leads/', views.leads, name='leads'),
    path('a/<uuid:account_id>/leads/<uuid:lead_id>/', views.lead_detail, name='lead_detail'),
    path('a/<uuid:account_id>/links/', views.links, name='links'),
    path('a/<uuid:account_id>/connections/', whatsapp_views.connections, name='connections'),
    path('a/<uuid:account_id>/connections/technical/', views.connection_technical, name='connection_technical'),
    path('settings/whatsapp/', whatsapp_views.workspace_settings, name='whatsapp_workspace'),
    path('a/<uuid:account_id>/connections/<uuid:connection_id>/connect/', whatsapp_views.connect_page, name='whatsapp_connect'),
    path('a/<uuid:account_id>/connections/<uuid:connection_id>/qr/', whatsapp_views.qr_code, name='whatsapp_qr'),
    path('a/<uuid:account_id>/connections/<uuid:connection_id>/finish/', whatsapp_views.finish_coexistence, name='whatsapp_finish'),
    path('a/<uuid:account_id>/connections/<uuid:connection_id>/prepare/', whatsapp_views.retry_setup, name='whatsapp_retry'),
    path('a/<uuid:account_id>/settings/', views.account_settings, name='account_settings'),
    path('a/<uuid:account_id>/export/', views.export_csv, name='export_csv'),
    path('r/<uuid:link_id>/', views.track, name='track'),
    path('hooks/zapi/<uuid:connection_id>/<str:token>/', views.zapi_webhook, name='zapi_webhook'),
    path('hooks/omni/<uuid:connection_id>/<str:token>/', views.omni_webhook, name='omni_webhook'),
    path('a/<uuid:account_id>/meta/', meta_views.meta_settings, name='meta_settings'),
    path('a/<uuid:account_id>/meta/connect/', meta_views.meta_connect, name='meta_connect'),
    path('integrations/meta/callback/', meta_views.meta_callback, name='meta_callback'),
    path('a/<uuid:account_id>/ads/', meta_views.ads_report, name='ads_report'),
    path('a/<uuid:account_id>/funnel/', funnel_views.board, name='funnel'),
    path('a/<uuid:account_id>/automation/', funnel_views.automation_settings, name='automation_settings'),
]
