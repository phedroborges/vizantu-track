import json
import re
import unicodedata
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .integrations import seal, unseal
from .models import (AutomationConfig, ConversionEvent, Dispatch, FunnelTransition,
                     Lead, MessageRule, Sale, Attribution, MetaIntegration)

EVENTS = ['Contact', 'Lead', 'LeadSubmitted', 'QualifiedLead', 'InitiateCheckout',
          'Schedule', 'Purchase', 'AddToCart', 'ViewContent', 'CompleteRegistration']


def normalize(text):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                            if not unicodedata.combining(c)).split())


def emit_event(lead, transition, sale=None):
    stage = transition.to_stage
    if not stage.event_name:
        return None
    key = f'sale:{sale.id}' if sale else f'stage:{lead.id}:{stage.id}'
    event = None
    if sale:
        event = lead.conversion_events.filter(event_name='Purchase', sale__isnull=True,
            status__in=['simulated', 'blocked']).order_by('occurred_at').first()
    if event:
        event.sale = sale
        event.save(update_fields=['sale'])
    else:
        config, _ = AutomationConfig.objects.get_or_create(account=lead.connection.account)
        event, created = ConversionEvent.objects.get_or_create(dedup_key=key, defaults={
            'lead': lead, 'transition': transition, 'sale': sale,
            'mode': config.mode,
            'event_name': stage.event_name, 'occurred_at': transition.occurred_at})
        if not created:
            return event
    prepare_event(event)
    return event


def prepare_event(event):
    from .services import meta_config
    account = event.lead.connection.account
    config, _ = AutomationConfig.objects.get_or_create(account=account)
    meta = meta_config(account)
    reason = ''
    if event.mode != config.mode:
        reason = 'Modo alterado desde o gatilho. Reavalie explicitamente; não há promoção automática de testes para produção.'
    elif event.event_name not in EVENTS:
        reason = 'Evento interno não reconhecido.'
    elif event.event_name == 'Purchase' and not event.sale_id:
        reason = 'Venda informada; falta registrar o valor real da compra.'
    elif config.mode != 'simulation':
        if event.event_name not in config.allowed_events:
            reason = 'Evento não liberado para este canal. Valide o suporte na Meta antes de enviar.'
        elif not all(meta.get(k, '').isdigit() for k in ('dataset_id', 'waba_id')) or not meta.get('access_token'):
            reason = 'Faltam dataset, WABA ou token de conversões com acesso aos ativos.'
        elif not re.fullmatch(r'v\d+\.\d+', settings.META_GRAPH_VERSION):
            reason = 'Versão da Graph API inválida.'
        elif config.mode == 'test' and not meta.get('test_event_code'):
            reason = 'Configure o código de teste do Gerenciador de Eventos.'
    attr = event.lead.attributions.filter(occurred_at__lte=event.occurred_at).exclude(ctwa_clid='').order_by('-occurred_at', '-id').first()
    if config.mode != 'simulation' and not reason and not attr:
        reason = 'Sem click ID de anúncio de WhatsApp. Não é possível presumir atribuição.'
    integration = MetaIntegration.objects.filter(account=account).first()
    waiting_ad = False
    if config.mode != 'simulation' and not reason and attr:
        if attr.enrichment_status in ('pending', 'processing'):
            reason, waiting_ad = 'Aguardando validação do anúncio e da conta proprietária.', True
        elif not integration or attr.enrichment_status != 'done' or attr.ad_account_id != integration.ad_account_id:
            reason = 'Não foi possível confirmar o anúncio na conta deste cliente.'
    if config.mode != 'simulation' and not reason and event.occurred_at < timezone.now() - timedelta(days=7):
        reason = 'Evento antigo; requer revisão da janela aceita pela Meta. Não altere o horário original.'
    if event.sale_id and Dispatch.objects.filter(sale_id=event.sale_id).exists():
        reason = 'Esta venda já tem envio no fluxo anterior. Não será gerada outra conversão.'
    event.status = 'waiting_ad' if waiting_ad else ('blocked' if reason else ('simulated' if config.mode == 'simulation' else 'pending'))
    event.reason = reason or ('Simulação: nenhum evento enviado à Meta.' if config.mode == 'simulation' else 'Aguardando envio pelo worker.')
    if event.status == 'pending':
        item = {'event_name': event.event_name, 'event_id': str(event.id),
            'event_time': int(event.occurred_at.timestamp()), 'action_source': 'business_messaging',
            'messaging_channel': 'whatsapp', 'user_data': {
                'whatsapp_business_account_id': meta['waba_id'], 'ctwa_clid': attr.ctwa_clid}}
        if event.sale_id:
            item['custom_data'] = {'value': float(event.sale.amount), 'currency': event.sale.currency}
        event.payload = {'data': [item]}
        if config.mode == 'test':
            event.payload['test_event_code'] = meta['test_event_code']
        event.delivery_config = seal({'dataset_id': meta['dataset_id'], 'waba_id': meta['waba_id'],
            'mode': config.mode, 'graph_version': settings.META_GRAPH_VERSION,
            'ad_account_id': integration.ad_account_id})
    event.updated_at = timezone.now()
    event.save()


def apply_message_rules(lead, message, initial=False):
    from .services import change_stage
    config = AutomationConfig.objects.filter(account=lead.connection.account).first()
    if not config or message.occurred_at + timedelta(milliseconds=1) < config.activated_at or message.edited:
        return
    if initial and message.direction == 'in':
        transition = FunnelTransition.objects.create(lead=lead, to_stage=lead.stage,
            message=message, origin='initial', occurred_at=message.occurred_at)
        emit_event(lead, transition)
    text = normalize(message.text)
    if message.kind != 'text':
        return
    for rule in MessageRule.objects.filter(account=lead.connection.account, active=True,
            direction=message.direction, activated_at__lte=message.occurred_at + timedelta(milliseconds=1)).select_related('to_stage', 'from_stage').order_by('priority', 'id'):
        if rule.from_stage_id and rule.from_stage_id != lead.stage_id:
            continue
        if rule.to_stage.archived or rule.to_stage.position <= lead.stage.position or lead.stage.won:
            continue
        phrase = normalize(rule.phrase)
        matched = bool(phrase) and (text == phrase if rule.match_type == 'exact' else phrase in text)
        if matched:
            change_stage(lead, rule.to_stage, None, message=message, rule=rule)
            break


@transaction.atomic
def retry_blocked(event, actor):
    from .models import Audit
    event = ConversionEvent.objects.select_for_update().get(pk=event.pk)
    if event.status != 'failed' and (event.status != 'blocked' or event.attempts):
        raise ValidationError('Só é possível reavaliar bloqueios nunca enviados ou recusas explícitas. Resultados incertos exigem conferência na Meta.')
    old_status, old_attempts = event.status, event.attempts
    event.attempts = 0
    event.next_attempt_at = timezone.now()
    event.mode = AutomationConfig.objects.get(account=event.lead.connection.account).mode
    prepare_event(event)
    Audit.objects.create(account=event.lead.connection.account, actor=actor,
        action='automation.event_prepared', object_id=str(event.id), detail={'previous_status': old_status, 'previous_attempts': old_attempts})


def deliver_event_once():
    from .services import locked_queryset, meta_config
    now = timezone.now()
    ConversionEvent.objects.filter(status='sending', updated_at__lt=now-timedelta(minutes=5)).update(
        status='review', reason='Worker interrompido: conferir recebimento na Meta antes de qualquer reenvio.', updated_at=now)
    with transaction.atomic():
        event = locked_queryset(ConversionEvent.objects.filter(status='pending', next_attempt_at__lte=now).order_by('occurred_at')).first()
        if not event:
            return False
        account = event.lead.connection.account
        config = AutomationConfig.objects.get(account=account)
        frozen = unseal(event.delivery_config)
        meta = meta_config(account)
        integration = MetaIntegration.objects.filter(account=account).first()
        if not account.active or not event.lead.connection.active or not integration or integration.ad_account_id != frozen['ad_account_id'] or config.mode != frozen['mode'] or meta.get('dataset_id') != frozen['dataset_id'] or meta.get('waba_id') != frozen['waba_id'] or not meta.get('access_token') or event.event_name not in config.allowed_events:
            event.status, event.reason = 'blocked', 'Envio suspenso: configuração, autorização ou conexão mudou.'
            event.save(update_fields=['status', 'reason'])
            return True
        if event.sale_id and Dispatch.objects.filter(sale_id=event.sale_id).exists():
            event.status, event.reason = 'blocked', 'Venda já possui despacho no fluxo anterior.'
            event.save(update_fields=['status', 'reason'])
            return True
        event.status, event.updated_at = 'sending', now
        event.attempts += 1
        event.save(update_fields=['status', 'attempts', 'updated_at'])
    status, reason = 'failed', 'Sem confirmação de recebimento.'
    try:
        request = Request(f'https://graph.facebook.com/{frozen["graph_version"]}/{frozen["dataset_id"]}/events',
            data=json.dumps(event.payload, allow_nan=False).encode(), method='POST',
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + meta['access_token']})
        with urlopen(request, timeout=20) as response:
            body = json.loads(response.read(65536))
        status, reason = ('sent', 'Meta aceitou o evento. A atribuição e a otimização dependem da plataforma.') if isinstance(body, dict) and type(body.get('events_received')) is int and body['events_received'] == 1 else ('review', 'Resposta sem confirmação. Conferir na Meta.')
    except HTTPError as exc:
        if exc.code == 429 and event.attempts < 5:
            status, reason = 'pending', 'Limite da Meta; nova tentativa com o mesmo identificador.'
        elif exc.code >= 500:
            status, reason = 'review', 'Erro do provedor; conferir recebimento antes de reenviar.'
        else:
            status, reason = 'failed', f'Meta HTTP {exc.code}. Conferir permissões, evento e configuração.'
    except (URLError, OSError, TimeoutError, ValueError):
        status, reason = 'review', 'Resultado incerto. Conferir na Meta; não haverá reenvio automático.'
    ConversionEvent.objects.filter(pk=event.pk).update(status=status, reason=reason, updated_at=timezone.now(),
        next_attempt_at=timezone.now()+timedelta(seconds=min(3600,60*2**event.attempts)))
    return True


def refresh_waiting_once():
    event = ConversionEvent.objects.filter(status='waiting_ad', updated_at__lte=timezone.now()-timedelta(seconds=10)).order_by('updated_at').first()
    if event is None:
        return False
    with transaction.atomic():
        event = ConversionEvent.objects.select_for_update().get(pk=event.pk)
        if event.status == 'waiting_ad':
            prepare_event(event)
    return True


def enrich_ad_once():
    from .integrations import graph
    from .services import locked_queryset
    with transaction.atomic():
        attr = locked_queryset(Attribution.objects.filter(enrichment_status='pending',
            lead__connection__account__active=True,
            lead__connection__account__meta__ad_account_id__startswith='act_').exclude(ad_id='')).first()
        if attr is None:
            return False
        integration = MetaIntegration.objects.get(account=attr.lead.connection.account)
        credentials = unseal(integration.credentials)
        try:
            if not attr.ad_id.isdigit() or not credentials.get('access_token'):
                raise ValidationError('Referência não consultável ou token ausente.')
            body = graph(attr.ad_id, credentials['access_token'], {'fields': 'id,name,account_id,campaign{id,name},adset{id,name}'}, credentials.get('app_secret', ''))
            if 'act_' + str(body.get('account_id')) != integration.ad_account_id:
                attr.enrichment_status = 'mismatch'
            else:
                attr.ad_account_id = integration.ad_account_id
                attr.ad_name = str(body.get('name', ''))[:500]
                attr.adset_name = str((body.get('adset') or {}).get('name', ''))[:500]
                attr.ad_campaign_name = str((body.get('campaign') or {}).get('name', ''))[:500]
                attr.enrichment_status = 'done'
        except (ValidationError, ValueError, TypeError):
            attr.enrichment_status = 'failed'
        attr.save(update_fields=['enrichment_status', 'ad_name', 'adset_name', 'ad_campaign_name', 'ad_account_id'])
    return True
