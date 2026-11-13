import secrets
import uuid
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from crm.models import Account, Attribution, Connection, Message, TrackableLink
from crm.services import create_account, ingest_message, record_sale, rotate_webhook


class Command(BaseCommand):
    help = 'Cria demonstração fictícia. Permitido somente com DEBUG=1.'

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError('Demonstração desabilitada em produção.')
        if get_user_model().objects.filter(username='demo').exists():
            self.stdout.write('Demonstração já existe. Para trocar a senha: python manage.py changepassword demo')
            return
        password = secrets.token_urlsafe(15)
        user = get_user_model().objects.create_superuser(username='demo', email='', password=password)
        account = create_account('Estúdio Aurora · demonstração', 'DEMO_AURORA', user)
        conn = Connection.objects.create(account=account, name='Comercial · demo', instance_id='DEMO-NOT-CONNECTED', phone='5511999990000')
        rotate_webhook(conn, user)
        TrackableLink.objects.create(connection=conn, name='Orçamento · Google', text='Olá! Gostaria de um orçamento.',
                                     source='google', medium='cpc', campaign='Pesquisa | Projetos')
        now = timezone.now()
        names = ['Marina Costa', 'Lucas Nascimento', 'Fernanda Dias', 'Rafael Mendes', 'Juliana Alves',
                 'Pedro Santos', 'Camila Rocha', 'Bruno Oliveira', 'Isabela Lima', 'André Ferreira', 'Ana Souza', 'Gabriel Silva']
        stages = list(account.stages.all())
        for index, name in enumerate(names):
            at = now - timedelta(days=index % 8, hours=index + 1)
            phone = f'551198888{index:04d}'
            body = {'instanceId': conn.instance_id, 'type': 'ReceivedCallback', 'phone': phone, 'fromMe': False,
                    'isGroup': False, 'momment': int(at.timestamp() * 1000), 'messageId': f'demo-in-{index}',
                    'chatName': name, 'text': {'message': 'Olá! Queria conhecer os serviços e receber uma proposta.'}}
            if index % 3 == 0:
                body['externalAdReply'] = {'sourceType': 'ad', 'sourceId': 'DEMO-AD', 'ctwaClid': 'DEMO-NOT-VALID', 'title': 'Projeto completo'}
            ingest_message(conn, body)
            lead = conn.leads.get(phone=phone)
            if index % 3 == 1:
                Attribution.objects.create(lead=lead, message=lead.messages.first(), source='google', medium='cpc',
                    campaign='Pesquisa | Projetos', evidence='message_code', occurred_at=at)
            lead.stage = stages[index % 5]
            lead.save(update_fields=['stage'])
            body.update({'fromMe': True, 'momment': int((at + timedelta(minutes=4)).timestamp() * 1000),
                         'messageId': f'demo-out-{index}', 'text': {'message': 'Olá! Obrigado pelo contato. Me conta um pouco sobre o seu projeto?'}})
            ingest_message(conn, body)
            if index in (0, 3, 6):
                record_sale(lead, Decimal(['2400.00', '3800.00', '1650.00'][index // 3]), uuid.uuid4(), user)
        create_account('Clínica Horizonte · demonstração', 'DEMO_HORIZONTE', user)
        self.stdout.write('DADOS FICTÍCIOS. Usuário: demo')
        self.stdout.write('Senha gerada (guarde agora): ' + password)
