import secrets
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Sum
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .forms import MetaForm
from .integrations import ad_accounts, graph, request_sync, seal, unseal
from .models import Attribution, Audit, MetaIntegration
from .views import access, page


def callback_url(request):
    return settings.META_REDIRECT_URI or request.build_absolute_uri(reverse('meta_callback'))


def safe_error(request, exc):
    messages.error(request, '; '.join(exc.messages))


@login_required
@require_http_methods(['GET', 'POST'])
def meta_settings(request, account_id):
    account, role = access(request, account_id, ['manager'])
    integration, _ = MetaIntegration.objects.get_or_create(account=account)
    credentials = unseal(integration.credentials)
    form = MetaForm(initial={key: credentials.get(key, '') for key in ('app_id', 'dataset_id', 'waba_id', 'test_event_code')})
    accounts = None
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'save':
                form = MetaForm(request.POST)
                if form.is_valid():
                    with transaction.atomic():
                        integration = MetaIntegration.objects.select_for_update().get(pk=integration.pk)
                        credentials = unseal(integration.credentials)
                        if integration.sync_status == 'running':
                            raise ValidationError('Aguarde a sincronização terminar antes de trocar as chaves.')
                        if form.cleaned_data['app_id'] != credentials.get('app_id', ''):
                            credentials.pop('access_token', None)
                            credentials.pop('app_secret', None)
                            integration.token_expires_at = None
                        for key, value in form.cleaned_data.items():
                            if key in ('app_secret', 'access_token', 'conversion_token') and not value:
                                continue
                            credentials[key] = value
                        if form.cleaned_data['access_token']:
                            integration.token_expires_at = None
                        integration.credentials = seal(credentials)
                        integration.save(update_fields=['credentials', 'token_expires_at'])
                        Audit.objects.create(account=account, actor=request.user, action='meta.credentials_saved', object_id=str(integration.pk))
                    messages.success(request, 'Configuração salva. Segredos não são exibidos novamente.')
                    return redirect('meta_settings', account_id=account.id)
                # Never echo bound password values in rendered HTML.
                form.data = form.data.copy()
                form.data['app_secret'] = form.data['access_token'] = form.data['conversion_token'] = ''
            elif action == 'list':
                accounts = ad_accounts(integration)
            elif action == 'select':
                selected = next((x for x in ad_accounts(integration) if x.get('id') == request.POST.get('ad_account_id')), None)
                if selected is None:
                    raise ValidationError('Esta conta não está autorizada para o token conectado.')
                with transaction.atomic():
                    integration = MetaIntegration.objects.select_for_update().get(pk=integration.pk)
                    if integration.sync_status == 'running':
                        raise ValidationError('Aguarde a sincronização terminar antes de trocar a conta.')
                    integration.ad_account_id = selected['id']
                    integration.ad_account_name = selected.get('name', '')[:200]
                    integration.currency = selected.get('currency', '')[:3]
                    integration.last_synced_at = None
                    integration.sync_status = 'idle'
                    integration.sync_result = ''
                    integration.save(update_fields=['ad_account_id', 'ad_account_name', 'currency', 'last_synced_at', 'sync_status', 'sync_result'])
                request_sync(integration)
                messages.success(request, 'Conta selecionada. Sincronização dos últimos 30 dias na fila.')
                return redirect('meta_settings', account_id=account.id)
            elif action == 'sync':
                request_sync(integration)
                messages.success(request, 'Sincronização na fila. Atualize a página em alguns instantes.')
                return redirect('meta_settings', account_id=account.id)
            elif action == 'disconnect':
                with transaction.atomic():
                    integration = MetaIntegration.objects.select_for_update().get(pk=integration.pk)
                    if integration.sync_status == 'running':
                        raise ValidationError('Aguarde a sincronização terminar.')
                    credentials.pop('access_token', None)
                    credentials.pop('conversion_token', None)
                    from .models import AutomationConfig
                    AutomationConfig.objects.filter(account=account).update(mode='simulation')
                    integration.credentials = seal(credentials)
                    integration.token_expires_at = None
                    integration.sync_status = 'idle'
                    integration.save(update_fields=['credentials', 'token_expires_at', 'sync_status'])
                    Audit.objects.create(account=account, actor=request.user, action='meta.disconnected', object_id=str(integration.pk))
                messages.success(request, 'Token removido do Rastro. O histórico foi mantido.')
                return redirect('meta_settings', account_id=account.id)
            else:
                return HttpResponse('Ação inválida.', status=400)
        except ValidationError as exc:
            safe_error(request, exc)
    response = page(request, 'crm/meta.html', account, role, integration=integration, form=form,
        has_token=bool(credentials.get('access_token')), oauth_ready=bool(credentials.get('app_id') and credentials.get('app_secret')),
        callback=callback_url(request), ad_accounts=accounts)
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@require_POST
def meta_connect(request, account_id):
    account, _ = access(request, account_id, ['manager'])
    integration, _ = MetaIntegration.objects.get_or_create(account=account)
    credentials = unseal(integration.credentials)
    if not credentials.get('app_id') or not credentials.get('app_secret'):
        messages.error(request, 'Cadastre App ID e App Secret antes de autorizar.')
        return redirect('meta_settings', account_id=account.id)
    state = secrets.token_urlsafe(32)
    request.session['meta_oauth'] = {'state': state, 'account': str(account.id), 'user': request.user.pk,
        'at': timezone.now().timestamp(), 'redirect_uri': callback_url(request),
        'app_id': credentials['app_id']}
    return redirect(f'https://www.facebook.com/{settings.META_GRAPH_VERSION}/dialog/oauth?' + urlencode({
        'client_id': credentials['app_id'], 'redirect_uri': callback_url(request),
        'state': state, 'response_type': 'code', 'scope': 'ads_read', 'auth_type': 'rerequest'}))


@login_required
@require_GET
def meta_callback(request):
    pending = request.session.pop('meta_oauth', None)
    if not pending or not secrets.compare_digest(pending.get('state', '').encode(), request.GET.get('state', '').encode()) or pending.get('user') != request.user.pk or not 0 <= timezone.now().timestamp() - pending['at'] <= 600:
        return HttpResponse('Autorização expirada ou inválida. Inicie novamente no Rastro.', status=400)
    account, _ = access(request, pending['account'], ['manager'])
    integration = MetaIntegration.objects.get(account=account)
    if request.GET.get('error') or not request.GET.get('code'):
        messages.error(request, 'Autorização cancelada pelo Facebook. Nenhuma credencial foi alterada.')
        return redirect('meta_settings', account_id=account.id)
    credentials = unseal(integration.credentials)
    try:
        if credentials.get('app_id') != pending['app_id']:
            raise ValidationError('O app mudou durante a autorização. Comece novamente.')
        token = graph('oauth/access_token', '', {'client_id': credentials['app_id'],
            'client_secret': credentials['app_secret'], 'redirect_uri': pending['redirect_uri'], 'code': request.GET['code']})
        value = token.get('access_token')
        if not isinstance(value, str) or not value or len(value) > 8000:
            raise ValidationError('O Facebook não retornou um token válido.')
        credentials['access_token'] = value
        integration.credentials = seal(credentials)
        expires = token.get('expires_in')
        integration.token_expires_at = timezone.now() + timedelta(seconds=int(expires)) if expires else None
        integration.save(update_fields=['credentials', 'token_expires_at'])
        Audit.objects.create(account=account, actor=request.user, action='meta.authorized', object_id=str(integration.pk))
        messages.success(request, 'Facebook autorizado. Clique em Buscar contas e selecione a conta deste cliente.')
    except (ValidationError, ValueError, OverflowError) as exc:
        if isinstance(exc, ValidationError):
            safe_error(request, exc)
        else:
            messages.error(request, 'Resposta de autorização inválida. Tente novamente.')
    response = redirect('meta_settings', account_id=account.id)
    response['Cache-Control'] = 'no-store'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@login_required
@require_GET
def ads_report(request, account_id):
    account, role = access(request, account_id)
    integration = MetaIntegration.objects.filter(account=account).first()
    rows, totals = [], {}
    if integration and integration.ad_account_id:
        since = timezone.localdate() - timedelta(days=29)
        insights = integration.insights.filter(ad_account_id=integration.ad_account_id, date__gte=since)
        totals = insights.aggregate(spend=Sum('spend'), impressions=Sum('impressions'), clicks=Sum('clicks'))
        rows = list(insights.values('ad_id').annotate(
            spend=Sum('spend'), impressions=Sum('impressions'), clicks=Sum('clicks')).order_by('-spend'))
        names = {}
        for item in insights.order_by('-date', '-id').values('ad_id', 'ad_name', 'campaign_name', 'adset_name'):
            names.setdefault(item['ad_id'], item)
        attributed = Attribution.objects.filter(lead__connection__account=account,
            occurred_at__date__gte=since, occurred_at__date__lte=timezone.localdate()).exclude(ad_id='').values('ad_id').annotate(count=Count('lead_id', distinct=True))
        counts = {x['ad_id']: x['count'] for x in attributed}
        for row in rows:
            row.update(names[row['ad_id']])
            row['leads'] = counts.get(row['ad_id'], 0)
            row['cost_per_lead'] = row['spend'] / Decimal(row['leads']) if row['leads'] else None
    return page(request, 'crm/ads.html', account, role, integration=integration, rows=rows, totals=totals)
