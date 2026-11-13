from django.core.management.base import BaseCommand, CommandError
from crm.models import Audit, WebhookEvent


class Command(BaseCommand):
    help = 'Recoloca um webhook com falha na fila, após corrigir a causa. Não reenvia conversões.'

    def add_arguments(self, parser):
        parser.add_argument('id', type=int)

    def handle(self, *args, **options):
        event = WebhookEvent.objects.filter(pk=options['id'], status='failed').first()
        if not event:
            raise CommandError('Webhook com falha não encontrado.')
        event.status, event.result = 'pending', ''
        event.save(update_fields=['status', 'result'])
        Audit.objects.create(account=event.connection.account, action='webhook.requeued', object_id=str(event.id))
        self.stdout.write('Webhook recolocado na fila.')
