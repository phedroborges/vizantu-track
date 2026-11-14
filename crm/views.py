import csv
import gzip
import io
import hashlib
import hmac
import json
import secrets
import uuid
import zlib
from datetime import timedelta

from django.contrib import messages
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import connection as db_connection, transaction
from django.db.models import Count, Q, Sum, OuterRef, Subquery, Exists
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .forms import AccountForm, ConnectionForm, LinkForm, SaleForm, StageForm, OmniForm, DealValueForm
from .integrations import omni_call, register_omni, seal, unseal, verify_omni
from .models import (Account, Attribution, Audit, Click, Connection, Dispatch, Lead,
                     Membership, Sale, Stage, TrackableLink, WebhookEvent, MetaIntegration)
from .services import (change_stage, create_account, queue_conversion, record_sale,
                       rotate_webhook)


def available_accounts(user):
    accounts = Account.objects.filter(active=True)
    return accounts if user.is_superuser else accounts.filter(memberships__user=user)


def access(request, account_id, roles=None):
    account = get_object_or_404(available_accounts(request.user), pk=account_id)
    role = 'manager' if request.user.is_superuser else account.memberships.get(user=request.user).role
    if roles and role not in roles:
        raise PermissionDenied('Seu perfil não permite esta ação.')
    return account, role


def page(request, template, account, role, **context):
    return render(request, template, {'account': account, 'role': role, 'can_edit': role != 'viewer',
        'is_manager': role == 'manager', 'accounts': available_accounts(request.user).order_by('name'), **context})


def lead_query(account):
    first = Attribution.objects.filter(lead_id=OuterRef('pk')).order_by('occurred_at', 'id')
    return Lead.objects.filter(connection__account=account).select_related('connection', 'stage').annotate(
        meta_origin=Exists(Attribution.objects.filter(lead_id=OuterRef('pk')).exclude(ad_id='')),
        origin=Subquery(first.values('source')[:1]), campaign_name=Subquery(first.values('campaign')[:1]))


def filter_leads(queryset, params):
    q = params.get('q', '')[:150].strip()
    if q:
        queryset = queryset.filter(Q(name__icontains=q) | Q(phone__icontains=q))
    stage = params.get('stage', '')
    if stage.isdigit():
        queryset = queryset.filter(stage_id=int(stage))
    origin = params.get('source', '')[:200]
    if origin == '__unknown':
        queryset = queryset.filter(origin__isnull=True)
    elif origin:
        queryset = queryset.filter(origin=origin)
    for key, lookup in [('start', 'first_message_at__date__gte'), ('end', 'first_message_at__date__lte')]:
        try:
            date = parse_date(params.get(key, ''))
        except ValueError:
            date = None
        if date:
            queryset = queryset.filter(**{lookup: date})
    return queryset


@require_GET
def health(request):
    with db_connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        cursor.fetchone()
    return JsonResponse({'status': 'ok'})


@login_required
def home(request):
    accounts = list(available_accounts(request.user).select_related('meta').annotate(
        number_count=Count('connections', filter=Q(connections__active=True, connections__last_received_at__isnull=False), distinct=True),
        lead_count=Count('connections__leads', distinct=True),
        tracked_count=Count('connections__leads__attributions__lead_id', distinct=True),
        link_count=Count('connections__links', filter=Q(connections__links__active=True, connections__active=True) & ~Q(connections__phone=''), distinct=True)
    ).order_by('name'))
    return render(request, 'crm/home.html', {'accounts': accounts,
        'clients': [x for x in accounts if x.integration_key not in ('DEMO_AURORA', 'DEMO_HORIZONTE')],
        'demos': [x for x in accounts if x.integration_key in ('DEMO_AURORA', 'DEMO_HORIZONTE')]})


@login_required
@require_http_methods(['GET', 'POST'])
def account_new(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    form = AccountForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        account = create_account(form.cleaned_data['name'], form.cleaned_data['integration_key'], request.user)
        return redirect('integrations', account_id=account.id)
    return render(request, 'crm/new_account.html', {'form': form, 'accounts': available_accounts(request.user)})


@login_required
@require_GET
def dashboard(request, account_id):
    account, role = access(request, account_id)
    since = timezone.now() - timedelta(days=30)
    new_leads = lead_query(account).filter(first_message_at__gte=since)
    total = new_leads.count()
    tracked = new_leads.filter(origin__isnull=False).count()
    sales = Sale.objects.filter(lead__connection__account=account, occurred_at__gte=since)
    sources = list(new_leads.order_by().values('origin').annotate(total=Count('pk')).order_by('-total')[:8])
    for row in sources:
        row['percent'] = round(row['total'] / max(1, total) * 100)
    stages = account.stages.annotate(total=Count('leads', filter=Q(leads__first_message_at__gte=since)))
    return page(request, 'crm/dashboard.html', account, role, total=total, tracked=tracked,
        tracked_percent=round(tracked / max(1, total) * 100), unknown=total - tracked,
        revenue=sales.aggregate(value=Sum('amount'))['value'] or 0, sales_count=sales.count(),
        stages=stages, sources=sources, latest=lead_query(account)[:6],
        whatsapp_ready=account.connections.filter(active=True, last_received_at__isnull=False).exists(),
        tracking_ready=TrackableLink.objects.filter(connection__account=account, connection__active=True, active=True).exclude(connection__phone='').exists(),
        meta_synced=MetaIntegration.objects.filter(account=account, last_synced_at__isnull=False).exclude(ad_account_id='').exists(),
        pending=WebhookEvent.objects.filter(connection__account=account, status='pending').count(),
        failed=WebhookEvent.objects.filter(connection__account=account, status='failed').count(),
        dispatches=Dispatch.objects.filter(sale__lead__connection__account=account).select_related('sale__lead').order_by('-updated_at')[:6])


@login_required
@require_GET
def leads(request, account_id):
    account, role = access(request, account_id)
    queryset = filter_leads(lead_query(account), request.GET)
    origins = lead_query(account).exclude(origin__isnull=True).order_by('origin').values_list('origin', flat=True).distinct()
    return page(request, 'crm/leads.html', account, role, stages=account.stages.all(),
        origins=origins, count=queryset.count(), items=Paginator(queryset, 30).get_page(request.GET.get('page')),
        query=request.GET.urlencode(), export_query=request.GET.urlencode(),
        filters={k: request.GET.get(k, '') for k in ('q', 'stage', 'source', 'start', 'end')})


@login_required
@require_http_methods(['GET', 'POST'])
def lead_detail(request, account_id, lead_id):
    account, role = access(request, account_id)
    lead = get_object_or_404(lead_query(account), pk=lead_id)
    sale_form = SaleForm(initial={'request_id': uuid.uuid4()})
    if request.method == 'POST':
        if role == 'viewer':
            raise PermissionDenied
        action = request.POST.get('action')
        try:
            if action == 'stage':
                stage_id = request.POST.get('stage', '')
                stage = get_object_or_404(account.stages, pk=int(stage_id) if stage_id.isdigit() else 0)
                change_stage(lead, stage, request.user)
                messages.success(request, 'Etapa atualizada.')
            elif action == 'sale':
                sale_form = SaleForm(request.POST)
                if sale_form.is_valid():
                    record_sale(lead, sale_form.cleaned_data['amount'], sale_form.cleaned_data['request_id'], request.user)
                    messages.success(request, 'Venda registrada. Confira o evento do funil e seu estado de envio.')
                else:
                    return detail_page(request, account, role, lead, sale_form)
            elif action == 'deal_value':
                form = DealValueForm(request.POST, instance=lead)
                if form.is_valid():
                    form.save()
                    pending_sale = lead.conversion_events.filter(event_name='Purchase', sale__isnull=True).first()
                    if lead.stage.won and lead.deal_value and pending_sale:
                        record_sale(lead, lead.deal_value, uuid.uuid5(pending_sale.id, 'sale'), request.user)
                    messages.success(request, 'Valor negociado atualizado.')
                else:
                    messages.error(request, 'Informe um valor positivo com até duas casas decimais.')
            elif action == 'conversion':
                if role != 'manager':
                    raise PermissionDenied
                if request.POST.get('confirm') != 'yes':
                    raise ValidationError('Confirme o envio externo da conversão.')
                sale = get_object_or_404(lead.sales, pk=request.POST.get('sale_id'))
                queue_conversion(sale, request.user)
                messages.success(request, 'Conversão colocada na fila de envio para a Meta.')
            else:
                return HttpResponse('Ação inválida.', status=400)
        except (ValidationError, ValueError) as exc:
            messages.error(request, '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
        return redirect('lead_detail', account_id=account.id, lead_id=lead.id)
    return detail_page(request, account, role, lead, sale_form)


def detail_page(request, account, role, lead, sale_form):
    return page(request, 'crm/lead_detail.html', account, role, lead=lead, stages=account.stages.filter(archived=False),
        deal_form=DealValueForm(instance=lead), conversion_events=lead.conversion_events.order_by('-occurred_at'),
        transitions=lead.transitions.select_related('from_stage', 'to_stage', 'rule', 'actor').order_by('-occurred_at')[:30],
        sale_form=sale_form, items=Paginator(lead.messages.order_by('-occurred_at', '-id'), 50).get_page(request.GET.get('page')),
        attributions=lead.attributions.all(), sales=lead.sales.select_related('operator', 'dispatch').order_by('-occurred_at'),
        history=account.audit.filter(object_id=str(lead.id)).order_by('-created_at')[:30])


@login_required
@require_http_methods(['GET', 'POST'])
def links(request, account_id):
    account, role = access(request, account_id)
    form = LinkForm(request.POST or None, account=account)
    if request.method == 'POST':
        if role != 'manager':
            raise PermissionDenied
        if request.POST.get('action') == 'toggle':
            link = get_object_or_404(TrackableLink, pk=request.POST.get('link_id'), connection__account=account)
            link.active = not link.active
            link.save(update_fields=['active'])
            Audit.objects.create(account=account, actor=request.user, action='link.toggled', object_id=str(link.id))
            return redirect('links', account_id=account.id)
        if form.is_valid():
            link = form.save()
            Audit.objects.create(account=account, actor=request.user, action='link.created', object_id=str(link.id))
            return redirect('links', account_id=account.id)
    rows = TrackableLink.objects.filter(connection__account=account).select_related('connection').annotate(
        click_count=Count('clicks', distinct=True), match_count=Count('clicks', filter=Q(clicks__matched_lead__isnull=False), distinct=True))
    for link in rows:
        path = reverse('track', args=[link.id])
        link.url = settings.PUBLIC_BASE_URL + path if settings.PUBLIC_BASE_URL else request.build_absolute_uri(path)
    return page(request, 'crm/links.html', account, role, form=form, links=rows,
                has_destination=form.fields['connection'].queryset.exists(), public_links=bool(settings.PUBLIC_BASE_URL))


@login_required
@require_http_methods(['GET', 'POST'])
def connection_technical(request, account_id):
    if not request.user.is_superuser:
        raise PermissionDenied
    account, role = access(request, account_id, roles=['manager'])
    form = ConnectionForm(request.POST or None)
    webhook_url = None
    if request.method == 'POST':
        action = request.POST.get('action', 'create')
        if action in ('omni_save', 'omni_register', 'omni_check'):
            conn = get_object_or_404(account.connections, pk=request.POST.get('connection_id'), provider='omni')
            try:
                if action == 'omni_save':
                    omni_form = OmniForm(request.POST)
                    if not omni_form.is_valid():
                        raise ValidationError('Secret Key inválida.')
                    values = unseal(conn.credentials)
                    if omni_form.cleaned_data['secret_key']:
                        values['secret_key'] = omni_form.cleaned_data['secret_key']
                    conn.credentials = seal(values)
                    if omni_form.cleaned_data['phone']:
                        conn.phone = omni_form.cleaned_data['phone']
                    conn.save(update_fields=['credentials', 'phone'])
                    messages.success(request, 'Chave Omni salva no servidor.')
                elif action == 'omni_register':
                    if not conn.phone:
                        raise ValidationError('Informe o telefone do canal antes de registrar o webhook.')
                    webhook_url = register_omni(conn, settings.PUBLIC_BASE_URL, request.user)
                    messages.success(request, 'Webhook Omni registrado em formato Z_API, com assinatura e autenticação. Outros webhooks foram preservados.')
                else:
                    values = unseal(conn.credentials)
                    result = omni_call(conn.instance_id, values.get('secret_key', ''), '/zapi/qr-code/image')
                    messages.success(request, 'WhatsApp conectado.' if result.get('connected') else 'O canal ainda precisa ser conectado no painel Omni.')
            except ValidationError as exc:
                messages.error(request, '; '.join(exc.messages))
            form = ConnectionForm()
        elif action in ('rotate', 'toggle'):
            conn = get_object_or_404(account.connections, pk=request.POST.get('connection_id'))
            if action == 'toggle':
                conn.active = not conn.active
                conn.save(update_fields=['active'])
                Audit.objects.create(account=account, actor=request.user, action='connection.toggled', object_id=str(conn.id))
                return redirect('connections', account_id=account.id)
            token = rotate_webhook(conn, request.user)
            route = 'omni_webhook' if conn.provider == 'omni' else 'zapi_webhook'
            webhook_url = request.build_absolute_uri(reverse(route, args=[conn.id, token]))
            if conn.provider == 'omni':
                values = unseal(conn.credentials)
                values['webhook_token'] = token
                conn.credentials = seal(values)
                conn.save(update_fields=['credentials'])
        elif action == 'create' and form.is_valid():
            with transaction.atomic():
                conn = form.save(commit=False)
                conn.account = account
                conn.token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
                conn.save()
                token = rotate_webhook(conn, request.user)
                route = 'omni_webhook' if conn.provider == 'omni' else 'zapi_webhook'
                webhook_url = request.build_absolute_uri(reverse(route, args=[conn.id, token]))
                if conn.provider == 'omni':
                    conn.credentials = seal({'webhook_token': token})
                    conn.save(update_fields=['credentials'])
            form = ConnectionForm()
    response = page(request, 'crm/connection_technical.html', account, role, form=form,
        connections=account.connections.all(), webhook_url=webhook_url,
        events=WebhookEvent.objects.filter(connection__account=account).select_related('connection').order_by('-created_at')[:15])
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@require_http_methods(['GET', 'POST'])
def account_settings(request, account_id):
    account, role = access(request, account_id, roles=['manager'])
    form = StageForm()
    if request.method == 'POST':
        if request.POST.get('action') == 'member':
            from django.contrib.auth import get_user_model
            user = get_user_model().objects.filter(username=request.POST.get('username', ''), is_active=True).first()
            selected_role = request.POST.get('role')
            if user and selected_role in dict(Membership.ROLES):
                if user == request.user and selected_role != 'manager':
                    messages.error(request, 'Você não pode remover seu próprio acesso de gestor.')
                else:
                    Membership.objects.update_or_create(account=account, user=user, defaults={'role': selected_role})
                    Audit.objects.create(account=account, actor=request.user, action='member.updated', object_id=str(user.id), detail={'role': selected_role})
                    messages.success(request, 'Acesso atualizado.')
            else:
                messages.error(request, 'Usuário ativo não encontrado ou perfil inválido. Cadastre o usuário na administração.')
            return redirect('account_settings', account_id=account.id)
        else:
            form = StageForm(request.POST)
            if form.is_valid():
                stage = form.save(commit=False)
                stage.account = account
                try:
                    stage.full_clean()
                    stage.save()
                except ValidationError as exc:
                    form.add_error(None, '; '.join(exc.messages))
                else:
                    Audit.objects.create(account=account, actor=request.user, action='stage.created', object_id=str(stage.id))
                    return redirect('account_settings', account_id=account.id)
    return page(request, 'crm/settings.html', account, role, form=form, stages=account.stages.all(),
        members=account.memberships.select_related('user'), roles=Membership.ROLES,
        audits=account.audit.select_related('actor').order_by('-created_at')[:20])


def csv_safe(value):
    text = '' if value is None else str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')) else text


@login_required
@require_GET
def export_csv(request, account_id):
    account, role = access(request, account_id)
    queryset = filter_leads(lead_query(account), request.GET)
    class Echo:
        def write(self, value):
            return value
    writer = csv.writer(Echo(), delimiter=';')
    def rows():
        yield '\ufeff'
        yield writer.writerow(['Contato', 'Telefone', 'WhatsApp', 'Etapa', 'Origem (primeiro toque)', 'Campanha', 'Primeira mensagem', 'Última mensagem'])
        for lead in queryset.iterator(chunk_size=500):
            yield writer.writerow([csv_safe(value) for value in [lead.name, lead.phone, lead.connection.name,
                lead.stage.name, lead.origin or 'Não rastreado', lead.campaign_name,
                lead.first_message_at.isoformat(), lead.last_message_at.isoformat()]])
    Audit.objects.create(account=account, actor=request.user, action='report.exported', object_id=str(account.id))
    return StreamingHttpResponse(rows(), content_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="conversas.csv"', 'Cache-Control': 'no-store'})


@require_GET
def track(request, link_id):
    link = get_object_or_404(TrackableLink.objects.select_related('connection__account'),
        pk=link_id, active=True, connection__active=True, connection__account__active=True)
    if not link.connection.phone:
        return HttpResponse('A conexão ainda precisa de um telefone válido.', status=409)
    values = {key: request.GET.get(param, default)[:limit] for key, param, default, limit in [
        ('source', 'utm_source', link.source, 200), ('medium', 'utm_medium', link.medium, 200),
        ('campaign', 'utm_campaign', link.campaign, 200), ('content', 'utm_content', '', 200),
        ('gclid', 'gclid', '', 500), ('fbclid', 'fbclid', '', 500)]}
    click = Click.objects.create(link=link, **values)
    from urllib.parse import urlencode
    destination = f'https://wa.me/{link.connection.phone}?' + urlencode({'text': link.text + f' [R:{click.id.hex}]'})
    response = redirect(destination)
    response['Cache-Control'] = 'no-store, private'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@csrf_exempt
@require_POST
def zapi_webhook(request, connection_id, token):
    conn = get_object_or_404(Connection.objects.select_related('account'), pk=connection_id, provider='zapi', active=True, account__active=True)
    if not hmac.compare_digest(conn.token_hash, hashlib.sha256(token.encode()).hexdigest()):
        return JsonResponse({'error': 'unauthorized'}, status=401)
    if request.content_type != 'application/json':
        return JsonResponse({'error': 'expected_json'}, status=415)
    try:
        raw = request.body
        if len(raw) > 512 * 1024:
            return JsonResponse({'error': 'payload_too_large'}, status=413)
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid_json'}, status=400)
    if not isinstance(payload, dict) or payload.get('instanceId') != conn.instance_id:
        return JsonResponse({'error': 'instance_mismatch'}, status=400)
    if payload.get('connectedPhone') and payload['connectedPhone'] != conn.phone:
        return JsonResponse({'error': 'phone_mismatch'}, status=400)
    with transaction.atomic():
        _, created = WebhookEvent.objects.get_or_create(connection=conn, digest=hashlib.sha256(raw).hexdigest(), defaults={'payload': payload})
        Connection.objects.filter(pk=conn.pk).update(last_received_at=timezone.now())
    return JsonResponse({'received': True, 'duplicate': not created})


@csrf_exempt
@require_POST
def omni_webhook(request, connection_id, token):
    conn = get_object_or_404(Connection.objects.select_related('account'), pk=connection_id,
                            provider='omni', active=True, account__active=True)
    if not hmac.compare_digest(conn.token_hash, hashlib.sha256(token.encode()).hexdigest()):
        return JsonResponse({'error': 'unauthorized'}, status=401)
    credentials = unseal(conn.credentials)
    if not hmac.compare_digest(request.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
        return JsonResponse({'error': 'unauthorized'}, status=401)
    if request.content_type != 'application/json':
        return JsonResponse({'error': 'expected_json'}, status=415)
    try:
        raw = request.body
        encoding = request.headers.get('Content-Encoding', '').lower()
        if encoding == 'gzip':
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                raw = stream.read(512 * 1024 + 1)
        elif encoding not in ('', 'identity'):
            return JsonResponse({'error': 'unsupported_encoding'}, status=415)
        if len(raw) > 512 * 1024:
            return JsonResponse({'error': 'payload_too_large'}, status=413)
        signing_secret = credentials.get('signing_secret')
        if not signing_secret or not verify_omni(raw, request.headers.get('X-Webhook-Signature', ''),
                                               request.headers.get('X-Idempotency-Key', ''), signing_secret):
            return JsonResponse({'error': 'invalid_signature'}, status=401)
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, OSError, EOFError, zlib.error):
        return JsonResponse({'error': 'invalid_json'}, status=400)
    if not isinstance(payload, dict) or payload.get('instanceId') != conn.instance_id:
        return JsonResponse({'error': 'channel_mismatch_use_Z_API_format'}, status=400)
    with transaction.atomic():
        conn = Connection.objects.select_for_update().get(pk=conn.pk)
        if payload.get('connectedPhone'):
            from .models import phone_validator
            if not isinstance(payload['connectedPhone'], str):
                return JsonResponse({'error': 'invalid_connected_phone'}, status=400)
            try:
                phone_validator(payload['connectedPhone'])
            except (ValidationError, TypeError):
                return JsonResponse({'error': 'invalid_connected_phone'}, status=400)
            if conn.phone and payload['connectedPhone'] != conn.phone:
                return JsonResponse({'error': 'phone_mismatch'}, status=400)
            if not conn.phone:
                conn.phone = payload['connectedPhone']
                conn.setup_status = 'connected'
                conn.save(update_fields=['phone', 'setup_status'])
                TrackableLink.objects.get_or_create(connection=conn, name='Link principal', defaults={
                    'source': 'link', 'medium': 'whatsapp', 'text': 'Olá! Quero saber mais.'})
        _, created = WebhookEvent.objects.get_or_create(connection=conn, digest=hashlib.sha256(raw).hexdigest(), defaults={'payload': payload})
        Connection.objects.filter(pk=conn.pk).update(last_received_at=timezone.now())
    return JsonResponse({'received': True, 'duplicate': not created})
