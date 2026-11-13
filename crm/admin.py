from django.contrib import admin
from .models import Account, Membership, Connection, Stage, Lead, Message, Attribution, Sale, Dispatch, Audit, WebhookEvent


class RestrictedAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in (Account, Connection, Stage, Lead, Message, Attribution, Sale, Dispatch, Audit, WebhookEvent):
    admin.site.register(model, RestrictedAdmin)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ['account', 'user', 'role']

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


admin.site.site_header = 'Rastro · Administração'
admin.site.site_title = 'Rastro'
