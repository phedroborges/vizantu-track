import os
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Cria o administrador inicial a partir do ambiente, sem modificar usuários existentes.'

    def handle(self, *args, **options):
        username = os.getenv('DJANGO_SUPERUSER_USERNAME', '')
        if not username:
            return
        User = get_user_model()
        if User.objects.filter(username=username).exists():
            return
        password = os.getenv('DJANGO_SUPERUSER_PASSWORD', '')
        user = User(username=username, is_staff=True, is_superuser=True)
        try:
            user.full_clean(exclude=['password', 'last_login', 'email'])
            validate_password(password, user)
        except ValidationError as exc:
            raise CommandError('Credenciais iniciais inválidas: ' + '; '.join(exc.messages)) from exc
        user.set_password(password)
        user.save()
        self.stdout.write('Administrador inicial criado. Remova as variáveis de bootstrap do ambiente.')
