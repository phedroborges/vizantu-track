import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from crm.services import process_inbox_once, dispatch_once


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
                if options['once']:
                    return
                if not worked and not sent:
                    time.sleep(2)
        except KeyboardInterrupt:
            self.stdout.write('Worker encerrado.')
