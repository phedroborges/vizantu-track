from django.core.management.base import BaseCommand
from crm.models import Account, AutomationConfig, MessageRule


class Command(BaseCommand):
    help = 'Prepara o funil existente em simulação, sem emitir eventos históricos.'

    def handle(self, *args, **options):
        for account in Account.objects.filter(active=True):
            config, _ = AutomationConfig.objects.get_or_create(account=account)
            if not config.prepared:
                for name, event in [('Novo contato', 'Contact'), ('Qualificado', 'Lead'), ('Proposta', 'InitiateCheckout'), ('Venda', 'Purchase')]:
                    account.stages.filter(name=name, event_name='').update(event_name=event)
                account.stages.filter(name='Perdido').update(archived=True)
                config.prepared = True
                config.save(update_fields=['prepared'])
            sale_stage = account.stages.filter(won=True, archived=False).first()
            if sale_stage:
                MessageRule.objects.get_or_create(account=account, name='Compra confirmada (exemplo)', defaults={
                    'phrase': 'obrigado pela compra', 'direction': 'out', 'to_stage': sale_stage, 'active': False})
        self.stdout.write('Funis preparados. Regras de exemplo pausadas; nenhum histórico foi enviado.')
