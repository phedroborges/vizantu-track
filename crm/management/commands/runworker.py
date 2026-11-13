import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from crm.services import process_inbox_once, dispatch_once
from crm.integrations import sync_ads_once
from crm.automation import deliver_event_once, enrich_ad_once, refresh_waiting_once


class Command(BaseCommand):
    help = 'Processa mensagens e conversões da fila persistida no PostgreSQL.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        try:
            while True:
                close_old_connections()
                worked = process_inbox_once()
                sent = dispatch_once()
                synced = sync_ads_once()
                enriched = enrich_ad_once()
                refreshed = refresh_waiting_once()
                automated = deliver_event_once()
                if options['once']:
                    return
                if not worked and not sent and not synced and not automated and not enriched and not refreshed:
                    time.sleep(2)
        except KeyboardInterrupt:
            self.stdout.write('Worker encerrado.')
