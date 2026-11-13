from django.contrib import admin
from django.contrib.auth import views as auth
from django.urls import path
from crm import views

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
]
