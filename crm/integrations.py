"""Provider calls stay on the server; neither tokens nor provider bodies enter logs."""
import base64
import hashlib
import hmac
import json
import re
import os
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen, HTTPRedirectHandler, build_opener

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import AdInsight, Audit, MetaIntegration, OmniWorkspace


def omni_workspace():
    config = OmniWorkspace.objects.filter(pk=1).first()
    values = unseal(config.credentials) if config else {}
    return {'secret_key': os.getenv('OMNI_SECRET_KEY') or values.get('secret_key', ''),
            'public_key': os.getenv('OMNI_PUBLIC_KEY') or values.get('public_key', '')}


def omni_secret(connection):
    return unseal(connection.credentials).get('secret_key') or omni_workspace()['secret_key']


def seal(data):
    key = base64.urlsafe_b64encode(hashlib.sha256(('rastro-integrations:' + settings.SECRET_KEY).encode()).digest())
    return Fernet(key).encrypt(json.dumps(data).encode()).decode()


def unseal(value):
    if not value:
        return {}
    key = base64.urlsafe_b64encode(hashlib.sha256(('rastro-integrations:' + settings.SECRET_KEY).encode()).digest())
    try:
        return json.loads(Fernet(key).decrypt(value.encode()))
    except (InvalidToken, ValueError):
        raise ValidationError('Não foi possível abrir as credenciais. Restaure a SECRET_KEY original ou cadastre as chaves novamente.') from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api_call(url, token='', data=None, method='GET'):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        headers['Content-Type'] = 'application/json'
    request = Request(url, headers=headers, data=json.dumps(data).encode() if data is not None else None, method=method)
    try:
        with build_opener(NoRedirect).open(request, timeout=25) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValidationError('Resposta muito grande. Reduza o período de sincronização.')
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get('error'):
            raise ValidationError('O provedor recusou a consulta. Confira credenciais e permissões.')
        return body
    except HTTPError as exc:
        raise ValidationError(f'Provedor HTTP {exc.code}. Confira chave, permissões e disponibilidade; nenhuma mensagem foi enviada.') from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise ValidationError('Não foi possível consultar o provedor. Tente novamente em alguns minutos.') from None


def omni_call(channel_id, secret, suffix='', data=None, method='GET'):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', channel_id):
        raise ValidationError('ID de canal inválido.')
    return api_call(f'https://api.omni.z-api.io/v1/channels/{channel_id}{suffix}', secret, data, method)


def register_omni(connection, public_url, actor):
    from .services import rotate_webhook
    from urllib.parse import urlparse
    url = urlparse(public_url)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
        raise ValidationError('Informe somente a origem HTTPS pública do app, sem caminho.')
    secrets = unseal(connection.credentials)
    secret_key = omni_secret(connection)
    if not secret_key:
        raise ValidationError('Cadastre a Secret Key do workspace Omni em Conexões.')
    # Preserve other integrations. Updating this ID never alters a third-party webhook.
    if secrets.get('webhook_token'):
        token = secrets['webhook_token']
    else:
        token = rotate_webhook(connection, actor)
        secrets['webhook_token'] = token
    from django.urls import reverse
    destination = public_url.rstrip('/') + reverse('omni_webhook', args=[connection.id, token])
    secrets['webhook_url'] = destination
    connection.credentials = seal(secrets)
    connection.save(update_fields=['credentials'])
    data = {'url': destination, 'description': 'Rastro — rastreamento comercial',
            'events': ['MESSAGE_RECEIVED', 'MESSAGE_DELIVERY'] if connection.mode == 'coexistence' else ['RECEIVED_AND_DELIVERY'], 'payloadFormat': 'Z_API',
            'signing': True, 'auth': {'type': 'BEARER', 'token': token}}
    suffix = '/webhooks' + ('/' + connection.webhook_id if connection.webhook_id else '')
    result = omni_call(connection.instance_id, secret_key, suffix, data,
                       'PATCH' if connection.webhook_id else 'POST')
    if not result.get('id') or not result.get('secret'):
        raise ValidationError('O Omni não retornou ID e segredo de assinatura. Confira o webhook no painel antes de tentar novamente.')
    connection.webhook_id = result['id']
    secrets['signing_secret'] = result['secret']
    connection.credentials = seal(secrets)
    connection.save(update_fields=['webhook_id', 'credentials'])
    Audit.objects.create(account=connection.account, actor=actor, action='omni.webhook_registered', object_id=str(connection.pk))
    return destination


def verify_omni(raw, signature, idempotency, secret):
    try:
        parts = dict(x.strip().split('=', 1) for x in signature.split(','))
        stamp, digest = parts['t'], parts['v1']
        topic, partition, offset = idempotency.rsplit(':', 2)
        if not topic or not partition.isdigit() or not offset.isdigit() or abs(timezone.now().timestamp() - int(stamp)) > 300:
            return False
        canonical = f'{stamp}\n{topic}\n{partition}\n{offset}\n'.encode() + raw
        return hmac.compare_digest(hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest(), digest)
    except (KeyError, ValueError, TypeError):
        return False


def graph(path, token, params=None, app_secret=''):
    if not re.fullmatch(r'v\d+\.\d+', settings.META_GRAPH_VERSION):
        raise ValidationError('Versão da Graph API inválida.')
    query = dict(params or {})
    if token and app_secret:
        query['appsecret_proof'] = hmac.new(app_secret.encode(), token.encode(), hashlib.sha256).hexdigest()
    return api_call(f'https://graph.facebook.com/{settings.META_GRAPH_VERSION}/{path}?' + urlencode(query), token)


def graph_pages(path, credentials, params):
    rows, seen = [], set()
    query = dict(params, limit=200)
    for _ in range(100):
        body = graph(path, credentials['access_token'], query, credentials.get('app_secret', ''))
        batch = body.get('data')
        if not isinstance(batch, list) or not all(isinstance(x, dict) for x in batch):
            raise ValidationError('Resposta de anúncios em formato inesperado.')
        rows.extend(batch)
        paging = body.get('paging', {})
        if not paging.get('next'):
            return rows
        cursor = paging.get('cursors', {}).get('after')
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ValidationError('Paginação incompleta. Os dados anteriores foram mantidos.')
        seen.add(cursor)
        # Never follow provider-supplied URLs with our bearer token.
        query['after'] = cursor
    raise ValidationError('A conta excede 20 mil resultados por consulta. Os dados anteriores foram mantidos.')


def ad_accounts(integration):
    creds = unseal(integration.credentials)
    if not creds.get('access_token'):
        raise ValidationError('Conecte o Facebook ou cadastre um token com ads_read.')
    return graph_pages('me/adaccounts', creds, {'fields': 'id,name,currency,account_status'})


def request_sync(integration):
    with transaction.atomic():
        obj = MetaIntegration.objects.select_for_update().get(pk=integration.pk)
        if obj.sync_status in ('pending', 'running'):
            raise ValidationError('Já existe uma sincronização em andamento.')
        if not obj.ad_account_id or not unseal(obj.credentials).get('access_token'):
            raise ValidationError('Conecte o Facebook e selecione uma conta de anúncios.')
        obj.sync_status, obj.sync_result = 'pending', 'Aguardando o worker.'
        obj.save(update_fields=['sync_status', 'sync_result'])


def sync_ads_once():
    from .services import locked_queryset
    now = timezone.now()
    MetaIntegration.objects.filter(sync_status='running', sync_started_at__lt=now - timedelta(minutes=15)).update(
        sync_status='failed', sync_result='Sincronização interrompida. Solicite novamente.')
    with transaction.atomic():
        integration = locked_queryset(MetaIntegration.objects.filter(
            Q(sync_status='pending') | Q(sync_status='done', last_synced_at__lte=now - timedelta(hours=1)),
            account__active=True)).first()
        if not integration:
            return False
        integration.sync_status, integration.sync_started_at = 'running', now
        integration.save(update_fields=['sync_status', 'sync_started_at'])
    try:
        sync_ads(integration)
    except (ValidationError, ValueError, KeyError, TypeError, InvalidOperation, OverflowError):
        MetaIntegration.objects.filter(pk=integration.pk).update(sync_status='failed',
            sync_result='A Meta não entregou uma consulta completa. Confira token/permissões e tente novamente. Dados anteriores preservados.')
    return True


def sync_ads(integration):
    credentials = unseal(integration.credentials)
    until = timezone.localdate()
    since = until - timedelta(days=29)
    external = integration.ad_account_id
    if not re.fullmatch(r'act_\d+', external):
        raise ValidationError('Conta de anúncios inválida.')
    if integration.token_expires_at and integration.token_expires_at <= timezone.now():
        raise ValidationError('Token expirado. Autorize o Facebook novamente.')
    rows = graph_pages(f'{external}/insights', credentials, {'level': 'ad', 'time_increment': 1,
        'time_range': json.dumps({'since': since.isoformat(), 'until': until.isoformat()}),
        'fields': 'account_id,ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,spend,impressions,clicks,date_start,date_stop'})
    records = []
    seen = set()
    for row in rows:
        date = parse_date(row['date_start'])
        if not date or not since <= date <= until or row['date_stop'] != row['date_start'] or 'act_' + row['account_id'] != external:
            raise ValidationError('A Meta retornou período ou conta inesperada.')
        key = (date, row['ad_id'])
        if key in seen:
            raise ValidationError('A Meta retornou linhas duplicadas.')
        seen.add(key)
        spend = Decimal(row['spend'])
        impressions, clicks = int(row.get('impressions', 0)), int(row.get('clicks', 0))
        if not spend.is_finite() or spend < 0 or spend >= 10**12 or impressions < 0 or clicks < 0:
            raise ValidationError('Métricas inválidas.')
        fields = {}
        for name in ('ad_id', 'ad_name', 'adset_id', 'adset_name', 'campaign_id', 'campaign_name'):
            value = row[name]
            if not isinstance(value, str) or len(value) > (80 if name.endswith('_id') else 500):
                raise ValidationError('Identificação de anúncio inválida.')
            fields[name] = value
        records.append(AdInsight(integration=integration, ad_account_id=external, date=date,
                               spend=spend, impressions=impressions, clicks=clicks, **fields))
    with transaction.atomic():
        current = MetaIntegration.objects.select_for_update().get(pk=integration.pk)
        if current.ad_account_id != external or current.credentials != integration.credentials:
            raise ValidationError('Configuração alterada durante a sincronização.')
        current.insights.filter(ad_account_id=external, date__range=(since, until)).delete()
        AdInsight.objects.bulk_create(records, batch_size=200)
        current.sync_status, current.sync_result = 'done', f'{len(records)} linhas diárias sincronizadas (últimos 30 dias).'
        current.last_synced_at = timezone.now()
        current.save(update_fields=['sync_status', 'sync_result', 'last_synced_at'])
