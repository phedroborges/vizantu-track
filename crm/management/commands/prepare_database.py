from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


class Command(BaseCommand):
    help = 'Prepara o schema privado do app. Após migrations, --secure protege tabelas contra acesso por outras roles.'

    def add_arguments(self, parser):
        parser.add_argument('--secure', action='store_true')

    def handle(self, *args, **options):
        schema = settings.DATABASE_SCHEMA
        if not schema:
            return
        if connection.vendor != 'postgresql':
            raise CommandError('Schemas privados exigem PostgreSQL.')
        quoted = connection.ops.quote_name(schema)
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA IF NOT EXISTS {quoted}')
            cursor.execute(f'REVOKE ALL ON SCHEMA {quoted} FROM PUBLIC')
            if options['secure']:
                cursor.execute('SELECT tablename FROM pg_tables WHERE schemaname = %s', [schema])
                for (table,) in cursor.fetchall():
                    cursor.execute(f'ALTER TABLE {quoted}.{connection.ops.quote_name(table)} ENABLE ROW LEVEL SECURITY')
        self.stdout.write('Schema privado do aplicativo verificado.')
