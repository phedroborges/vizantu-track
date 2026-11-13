import hashlib
from datetime import timedelta
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from .models import LoginAttempt


class LoginThrottleMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method != 'POST' or request.path not in ('/login/', '/admin/login/'):
            return self.get_response(request)
        # Shared in PostgreSQL across Gunicorn workers. Account key also stops rotating IPs.
        username = request.POST.get('username', '').strip().casefold()[:150]
        keys = [hashlib.sha256(value.encode()).hexdigest() for value in
                ('user:' + username, 'ip:' + request.META.get('REMOTE_ADDR', ''))]
        now = timezone.now()
        for key in keys:
            with transaction.atomic():
                row, _ = LoginAttempt.objects.get_or_create(key=key)
                row = LoginAttempt.objects.select_for_update().get(key=key)
                if row.window_start < now - timedelta(minutes=15):
                    row.failures, row.window_start = 0, now
                if row.failures >= 20:
                    return HttpResponse('Muitas tentativas. Tente novamente em 15 minutos.', status=429,
                                        headers={'Retry-After': '900'})
                row.failures += 1
                row.save()
        response = self.get_response(request)
        if request.user.is_authenticated:
            LoginAttempt.objects.filter(key=keys[0]).delete()
        return response
