import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction, connection as db_connection
from django.utils import timezone
from .models import (Account, Attribution, Audit, Click, Connection, Dispatch, Lead,
                     Membership, Message, Sale, Stage, WebhookEvent, phone_validator)


@transaction.atomic
def create_account(name, key, user):
    account = Account(name=name, integration_key=key)
    account.full_clean()
    account.save()
    Membership.objects.create(account=account, user=user, role='manager')
    for position, (name, won) in enumerate([('Novo contato', False), ('Qualificado', False),
                                           ('Proposta', False), ('Venda', True)]):
        Stage.objects.create(account=account, name=name, position=position, won=won,
            event_name=['Contact', 'Lead', 'InitiateCheckout', 'Purchase'][position])
    from .models import AutomationConfig
    AutomationConfig.objects.create(account=account, prepared=True)
    Audit.objects.create(account=account, actor=user, action='client.created', object_id=str(account.id))
    return account


def rotate_webhook(connection, actor):
    token = secrets.token_urlsafe(32)
    connection.token_hash = hashlib.sha256(token.encode()).hexdigest()
    connection.save(update_fields=['token_hash'])
    Audit.objects.create(account=connection.account, actor=actor, action='webhook.rotated',
                         object_id=str(connection.id))
    return token


def limited_string(value, limit=200):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('Campo de texto inválido ou acima do limite.')
    return value


def parse_zapi(payload):
    if payload.get('type') != 'ReceivedCallback':
        return None
    for flag in ('fromMe', 'isGroup', 'isNewsletter', 'isStatusReply', 'isEdit'):
        if flag in payload and not isinstance(payload[flag], bool):
            raise ValueError(f'{flag} precisa ser booleano.')
    if any(payload.get(k) for k in ('isGroup', 'isNewsletter', 'isStatusReply', 'notification')):
        return None
    external_id = limited_string(payload.get('messageId'), 200)
    if not external_id or 'fromMe' not in payload:
        raise ValueError('Mensagem sem identificador ou direção.')
    phone = limited_string(payload.get('phone'), 40)
    # Never turn a WhatsApp LID into a phone number by stripping non-digits.
    phone_validator(phone)
    moment = payload.get('momment')
    if isinstance(moment, bool) or not isinstance(moment, (int, float)):
        raise ValueError('momment precisa ser um timestamp em milissegundos.')
    occurred = datetime.fromtimestamp(moment / 1000, dt_timezone.utc)
    if occurred.year < 2000 or occurred > timezone.now() + timedelta(days=1):
        raise ValueError('Timestamp fora do intervalo permitido.')
    kind, text = 'unknown', '[Tipo de mensagem ainda não interpretado]'
    if isinstance(payload.get('text'), dict):
        kind, text = 'text', limited_string(payload['text'].get('message', ''), 64000)
    else:
        for key, label in [('image', 'Imagem'), ('audio', 'Áudio'), ('video', 'Vídeo'),
                           ('document', 'Documento'), ('sticker', 'Figurinha'),
                           ('location', 'Localização'), ('contact', 'Contato')]:
            if isinstance(payload.get(key), dict):
                kind = key
                text = '[' + label + '] ' + limited_string(payload[key].get('caption', ''), 10000)
                break
    if payload.get('waitingMessage'):
        kind, text = 'waiting', '[Aguardando conteúdo da mensagem]'
    return {'external_id': external_id, 'phone': phone, 'occurred_at': occurred,
            'direction': 'out' if payload['fromMe'] else 'in', 'kind': kind, 'text': text,
            'name': limited_string(payload.get('chatName') or payload.get('senderName') or '', 150)}


@transaction.atomic
def ingest_message(connection, payload):
    if not connection.active or not connection.account.active:
        return 'ignored: conexão ou cliente inativo'
    data = parse_zapi(payload)
    if data is None:
        return 'ignored: evento não comercial'
    stage = connection.account.stages.filter(archived=False).first()
    if stage is None:
        raise ValueError('Cliente sem etapas comerciais.')
    lead, _ = Lead.objects.get_or_create(connection=connection, phone=data['phone'], defaults={
        'name': data['name'], 'stage': stage, 'first_message_at': data['occurred_at'],
        'last_message_at': data['occurred_at']})
    # Serializes first/last message updates, attribution and stage writes for this lead.
    lead = Lead.objects.select_for_update().get(pk=lead.pk)
    msg, created = Message.objects.get_or_create(lead=lead, external_id=data['external_id'], defaults={
        k: data[k] for k in ('direction', 'kind', 'text', 'occurred_at')})
    if not created:
        if payload.get('isEdit') or msg.kind == 'waiting':
            msg.text, msg.kind, msg.edited = data['text'], data['kind'], True
            msg.save(update_fields=['text', 'kind', 'edited'])
        return 'done: mensagem já recebida'
    lead.first_message_at = min(lead.first_message_at, msg.occurred_at)
    lead.last_message_at = max(lead.last_message_at, msg.occurred_at)
    if not lead.name and msg.direction == 'in':
        lead.name = data['name']
    lead.save(update_fields=['first_message_at', 'last_message_at', 'name'])
    if msg.direction == 'in' and not payload.get('isEdit'):
        record_attribution(lead, msg, payload)
    if not payload.get('isEdit'):
        from .automation import apply_message_rules
        apply_message_rules(lead, msg, initial=msg.direction == 'in' and not lead.messages.exclude(pk=msg.pk).filter(direction='in').exists())
    return 'done: mensagem registrada'


def record_attribution(lead, msg, payload):
    ad = payload.get('externalAdReply')
    ad_fields = {}
    if isinstance(ad, dict) and ad.get('sourceType') == 'ad' and ad.get('sourceId'):
        ad_fields = {'ad_id': limited_string(ad['sourceId']),
                     'ctwa_clid': limited_string(ad.get('ctwaClid', ''), 1000)}
    # A code is evidence only when received from the contact on the same instance.
    match = re.search(r'\[R:([0-9a-fA-F]{32})\]', msg.text)
    if match:
        click = Click.objects.select_for_update().filter(
            pk=match[1], link__connection=lead.connection, matched_lead__isnull=True,
            created_at__gte=msg.occurred_at - timedelta(days=30),
            created_at__lte=msg.occurred_at + timedelta(milliseconds=1)).first()
        if click:
            click.matched_lead = lead
            click.save(update_fields=['matched_lead'])
            Attribution.objects.create(lead=lead, click=click, message=msg, source=click.source or 'link',
                medium=click.medium, campaign=click.campaign, content=click.content,
                gclid=click.gclid, fbclid=click.fbclid, evidence='message_code', occurred_at=msg.occurred_at, **ad_fields)
            return
    if ad_fields:
        Attribution.objects.create(lead=lead, message=msg, source='meta', medium='ctwa',
            content=limited_string(ad.get('title', '')), evidence='ad_referral', occurred_at=msg.occurred_at, **ad_fields)


def locked_queryset(queryset):
    return queryset.select_for_update(skip_locked=True, of=('self',)) if db_connection.vendor == 'postgresql' else queryset.select_for_update()


def process_inbox_once():
    with transaction.atomic():
        event = locked_queryset(WebhookEvent.objects.filter(status='pending').order_by('id')).first()
        if event is None:
            return False
        try:
            result = ingest_message(event.connection, event.payload)
            event.status = 'ignored' if result.startswith('ignored') else 'done'
            event.result = result
        except (ValueError, TypeError, KeyError, OverflowError, OSError, ValidationError) as exc:
            event.status = 'failed'
            event.result = ('Payload não processado: ' + str(exc))[:300]
        event.processed_at = timezone.now()
        event.save(update_fields=['status', 'result', 'processed_at'])
        return True


@transaction.atomic
def change_stage(lead, stage, actor, message=None, rule=None):
    lead = Lead.objects.select_for_update().get(pk=lead.pk)
    if stage.account_id != lead.connection.account_id:
        raise ValidationError('Etapa pertence a outro cliente.')
    if stage.archived:
        raise ValidationError('Etapa arquivada.')
    if lead.stage_id == stage.pk:
        return
    previous_stage = lead.stage
    previous = previous_stage.name
    lead.stage = stage
    lead.save(update_fields=['stage'])
    if rule and stage.won and lead.deal_value is None and rule.fixed_value:
        lead.deal_value = rule.fixed_value
        lead.save(update_fields=['deal_value'])
    Audit.objects.create(account=lead.connection.account, actor=actor, action='stage.changed',
        object_id=str(lead.id), detail={'from': previous, 'to': stage.name})
    from .models import FunnelTransition
    from .automation import emit_event
    transition = FunnelTransition.objects.create(lead=lead, from_stage=previous_stage, to_stage=stage,
        actor=actor, message=message, rule=rule, origin='message' if message else 'manual',
        snapshot={'from': previous, 'to': stage.name, 'event': stage.event_name,
                  'phrase': rule.phrase if rule else '', 'direction': rule.direction if rule else '',
                  'rule_name': rule.name if rule else '', 'match_type': rule.match_type if rule else '',
                  'priority': rule.priority if rule else None,
                  'rule_version': rule.activated_at.isoformat() if rule else ''},
        occurred_at=message.occurred_at if message else timezone.now())
    emit_event(lead, transition)
    if message and stage.won and lead.deal_value and not lead.conversion_events.filter(event_name='Purchase', sale__isnull=False).exists():
        import uuid
        record_sale(lead, lead.deal_value, uuid.uuid5(transition.id, 'sale'), None)
    return transition


@transaction.atomic
def record_sale(lead, amount, request_id, actor):
    lead = Lead.objects.select_for_update().get(pk=lead.pk)
    existing = Sale.objects.filter(request_id=request_id).first()
    if existing:
        if existing.lead_id != lead.id or existing.amount != amount:
            raise ValidationError('Chave de registro já usada em outra venda.')
        return existing
    sale = Sale(lead=lead, amount=amount, request_id=request_id, operator=actor)
    sale.full_clean()
    sale.save()
    won = lead.connection.account.stages.filter(won=True).first()
    if won:
        change_stage(lead, won, actor)
        from .automation import emit_event
        from .models import FunnelTransition
        transition = lead.transitions.filter(to_stage=won).order_by('-occurred_at').first()
        if transition is None:
            transition = FunnelTransition.objects.create(lead=lead, to_stage=won, actor=actor, origin='manual')
        if won.event_name == 'Purchase':
            emit_event(lead, transition, sale=sale)
    Audit.objects.create(account=lead.connection.account, actor=actor, action='sale.created',
                         object_id=str(sale.id), detail={'amount': str(amount), 'currency': sale.currency})
    return sale


def meta_config(account):
    prefix = 'META_' + account.integration_key + '_'
    config = {key.lower(): os.getenv(prefix + key, '') for key in ('DATASET_ID', 'ACCESS_TOKEN', 'WABA_ID', 'TEST_EVENT_CODE')}
    from .models import MetaIntegration
    from .integrations import unseal
    integration = MetaIntegration.objects.filter(account=account).first()
    if integration:
        credentials = unseal(integration.credentials)
        for key in config:
            stored_key = 'conversion_token' if key == 'access_token' else key
            if credentials.get(stored_key):
                config[key] = credentials[stored_key]
    return config


@transaction.atomic
def queue_conversion(sale, actor):
    sale = Sale.objects.select_for_update().select_related('lead__connection__account').get(pk=sale.pk)
    if Dispatch.objects.filter(sale=sale).exists():
        raise ValidationError('Esta venda já tem um despacho. Confira o histórico antes de qualquer ação.')
    if hasattr(sale, 'automation_event'):
        event = sale.automation_event
        if event.status != 'simulated':
            raise ValidationError('Esta venda já tem evento no funil. Confira a fila de eventos para evitar duplicidade.')
        event.status, event.reason = 'cancelled', 'Substituído pelo envio manual autorizado desta venda.'
        event.save(update_fields=['status', 'reason'])
    account = sale.lead.connection.account
    config = meta_config(account)
    if not all(config[k] for k in ('dataset_id', 'access_token', 'waba_id')) or not re.fullmatch(r'v\d+\.\d+', settings.META_GRAPH_VERSION):
        raise ValidationError('Configure dataset, token, WABA e versão da Graph API no ambiente antes de enviar.')
    attr = sale.lead.attributions.filter(occurred_at__lte=sale.occurred_at).exclude(ctwa_clid='').order_by('-occurred_at', '-id').first()
    if not attr:
        raise ValidationError('Venda sem ctwaClid recebido da Z-API. A conversão Meta não pode ser inferida pelo clique do link.')
    payload = {'data': [{'event_name': 'Purchase', 'event_id': str(sale.id),
        'event_time': int(sale.occurred_at.timestamp()), 'action_source': 'business_messaging',
        'messaging_channel': 'whatsapp', 'user_data': {'whatsapp_business_account_id': config['waba_id'],
        'ctwa_clid': attr.ctwa_clid}, 'custom_data': {'value': float(sale.amount), 'currency': sale.currency}}]}
    if config['test_event_code']:
        payload['test_event_code'] = config['test_event_code']
    dispatch = Dispatch.objects.create(sale=sale, payload=payload)
    Audit.objects.create(account=account, actor=actor, action='conversion.queued', object_id=str(sale.id))
    return dispatch


def dispatch_once():
    now = timezone.now()
    # A crashed request may already have reached Meta. Do not blindly repeat purchases.
    Dispatch.objects.filter(status='sending', updated_at__lt=now - timedelta(minutes=5)).update(
        status='review', result='Worker interrompido. Conferir recebimento na Meta antes de reenviar.', updated_at=now)
    with transaction.atomic():
        dispatch = locked_queryset(Dispatch.objects.filter(status='pending', next_attempt_at__lte=now).order_by('id')).first()
        if not dispatch:
            return False
        dispatch.status = 'sending'
        dispatch.attempts += 1
        dispatch.updated_at = now
        dispatch.save(update_fields=['status', 'attempts', 'updated_at'])
    account = dispatch.sale.lead.connection.account
    config = meta_config(account)
    status, result = 'failed', 'Integração ausente ou cliente inativo.'
    if account.active and config['access_token'] and config['dataset_id'].isdigit() and re.fullmatch(r'v\d+\.\d+', settings.META_GRAPH_VERSION):
        request = Request(f'https://graph.facebook.com/{settings.META_GRAPH_VERSION}/{config["dataset_id"]}/events',
            data=json.dumps(dispatch.payload, allow_nan=False).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + config['access_token']}, method='POST')
        try:
            with urlopen(request, timeout=20) as response:
                body = json.loads(response.read(65536))
            if body.get('events_received') == 1:
                status, result = 'sent', 'Meta confirmou o recebimento de 1 evento. Atribuição de anúncio ainda depende da Meta.'
            else:
                status, result = 'review', 'Resposta sem confirmação explícita de recebimento. Conferir na Meta.'
        except HTTPError as exc:
            # Only a rate-limit response is retried automatically; no response bodies/tokens in logs.
            if exc.code == 429 and dispatch.attempts < 5:
                status, result = 'pending', 'Limite da Meta; nova tentativa agendada.'
            elif exc.code >= 500:
                status, result = 'review', f'Meta HTTP {exc.code}. Conferir recebimento antes de reenviar.'
            else:
                status, result = 'failed', f'Meta HTTP {exc.code}; revisar credenciais e configuração no Events Manager.'
        except (URLError, TimeoutError, OSError, ValueError):
            status, result = 'review', 'Sem confirmação de entrega; conferir na Meta antes de reenviar.'
    Dispatch.objects.filter(pk=dispatch.pk, status='sending').update(status=status, result=result,
        updated_at=timezone.now(), next_attempt_at=timezone.now() + timedelta(seconds=min(3600, 60 * 2 ** dispatch.attempts)))
    Audit.objects.create(account=account, action='conversion.' + status, object_id=str(dispatch.sale_id), detail={'attempt': dispatch.attempts})
    return True
