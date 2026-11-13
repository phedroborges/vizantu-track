from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from crm.models import WebhookEvent, LoginAttempt


class Command(BaseCommand):
    help = 'Remove somente payloads brutos já processados, após 30 dias. Sem --apply, apenas informa a contagem.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        events = WebhookEvent.objects.filter(status__in=['done', 'ignored'], created_at__lt=timezone.now() - timedelta(days=30))
        self.stdout.write(f'{events.count()} payloads brutos elegíveis; mensagens, vendas e auditoria permanecem.')
        if options['apply']:
            events.delete()
            LoginAttempt.objects.filter(window_start__lt=timezone.now() - timedelta(days=1)).delete()
            self.stdout.write('Limpeza concluída.')
