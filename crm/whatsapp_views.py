import base64
import hashlib
import json
import re
import secrets
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .forms import OmniWorkspaceForm, WhatsAppConnectForm
from .integrations import (api_call, omni_call, omni_secret, omni_workspace,
                           register_omni, seal, unseal)
from .models import Audit, Connection, OmniWorkspace
from .views import access, available_accounts, page


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def workspace_settings(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    obj, _ = OmniWorkspace.objects.get_or_create(pk=1)
    values = unseal(obj.credentials)
    form = OmniWorkspaceForm(initial={'public_key': values.get('public_key', '')})
    if request.method == 'POST':
        form = OmniWorkspaceForm(request.POST)
        if form.is_valid():
            if form.cleaned_data['secret_key']:
                values['secret_key'] = form.cleaned_data['secret_key']
            values['public_key'] = form.cleaned_data['public_key']
            obj.credentials = seal(values)
            obj.save(update_fields=['credentials'])
            messages.success(request, 'Integração WhatsApp salva. Os clientes já podem iniciar as modalidades habilitadas.')
            return redirect('whatsapp_workspace')
    config = omni_workspace()
    if config['secret_key']:
        form.fields['secret_key'].widget.attrs.update({
            'placeholder': '••••••••••••••••', 'autocomplete': 'new-password'})
        form.fields['secret_key'].help_text = 'Chave salva. Digite uma nova chave somente se quiser substituí-la.'
    return render(request, 'crm/whatsapp_workspace.html', {'form': form, 'has_secret': bool(config['secret_key']),
        'has_public': bool(config['public_key']), 'accounts': available_accounts(request.user),
        'sdk_origin': request.build_absolute_uri('/').rstrip('/')})


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def connections(request, account_id):
    account, role = access(request, account_id, ['manager'])
    config = omni_workspace()
    form = WhatsAppConnectForm(initial={'request_id': uuid.uuid4()})
    if request.method == 'POST':
        if request.POST.get('action') == 'toggle':
            conn = get_object_or_404(account.connections, pk=request.POST.get('connection_id'))
            conn.active = not conn.active
            conn.save(update_fields=['active'])
            Audit.objects.create(account=account, actor=request.user, action='connection.toggled', object_id=str(conn.id))
            return redirect('connections', account_id=account.id)
        form = WhatsAppConnectForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            if not config['secret_key'] or (data['mode'] == 'coexistence' and not config['public_key']):
                form.add_error(None, 'Esta modalidade aguarda liberação da equipe responsável. Entre em contato para concluir a configuração.')
            elif not settings.PUBLIC_BASE_URL.startswith('https://'):
                form.add_error(None, 'A equipe responsável precisa habilitar o endereço seguro de recebimento antes de conectar novos números.')
            else:
                with transaction.atomic():
                    conn, created = Connection.objects.get_or_create(pk=data['request_id'], defaults={
                        'account': account, 'name': data['name'], 'provider': 'omni', 'mode': data['mode'],
                        'instance_id': 'pending_' + data['request_id'].hex, 'setup_status': 'creating',
                        'token_hash': hashlib.sha256(secrets.token_bytes(32)).hexdigest()})
                    if conn.account_id != account.id:
                        raise PermissionDenied
                if created:
                    try:
                        result = api_call('https://api.omni.z-api.io/v1/channels', config['secret_key'],
                            {'name': f'{account.name} · {conn.name} · {str(conn.id)[:8]}',
                             'type': 'META_WHATSAPP' if conn.mode == 'coexistence' else 'Z_API_WHATSAPP'}, 'POST')
                        channel_id = result.get('id')
                        if not isinstance(channel_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', channel_id):
                            raise ValidationError('Resposta de criação sem ID de canal válido.')
                        conn.instance_id = channel_id
                        conn.credentials = seal({'channel_token': result.get('token', '')})
                        conn.setup_status = 'pending'
                        conn.save(update_fields=['instance_id', 'credentials', 'setup_status'])
                        register_omni(conn, settings.PUBLIC_BASE_URL, request.user)
                        Audit.objects.create(account=account, actor=request.user, action='whatsapp.channel_created', object_id=str(conn.id), detail={'mode': conn.mode})
                    except ValidationError:
                        conn.setup_status = 'error'
                        conn.save(update_fields=['setup_status'])
                        messages.error(request, 'Não foi possível preparar a conexão. A equipe responsável pode conferir e retomar esta tentativa sem recriar o canal.')
                return redirect('whatsapp_connect', account_id=account.id, connection_id=conn.id)
    return page(request, 'crm/connections.html', account, role, form=form,
        connections=account.connections.all(), qr_ready=bool(config['secret_key']),
        coexistence_ready=bool(config['secret_key'] and config['public_key']))


@login_required
@never_cache
@require_GET
def connect_page(request, account_id, connection_id):
    account, role = access(request, account_id, ['manager'])
    conn = get_object_or_404(account.connections, pk=connection_id, provider='omni')
    config = omni_workspace()
    ready = bool(conn.active and omni_secret(conn) and conn.webhook_id and not conn.instance_id.startswith('pending_'))
    return page(request, 'crm/whatsapp_connect.html', account, role, connection=conn, ready=ready,
        public_key=config['public_key'] if conn.mode == 'coexistence' else '',
        sdk_ready=bool(config['public_key']),
        connect_config={'publicKey': config['public_key'], 'channelId': conn.instance_id,
            'finishUrl': reverse('whatsapp_finish', args=[account.id, conn.id])})


@login_required
@never_cache
@require_POST
def qr_code(request, account_id, connection_id):
    account, _ = access(request, account_id, ['manager'])
    conn = get_object_or_404(account.connections, pk=connection_id, provider='omni', mode='qr', active=True)
    try:
        secret = omni_secret(conn)
        if not secret or not conn.webhook_id or conn.instance_id.startswith('pending_'):
            raise ValidationError('Conexão ainda não preparada.')
        result = omni_call(conn.instance_id, secret, '/zapi/qr-code/image')
        if result.get('connected') is True:
            conn.setup_status = 'connected'
            conn.save(update_fields=['setup_status'])
            return JsonResponse({'connected': True})
        qr = result.get('qrcode')
        if qr is None:
            return JsonResponse({'connected': False, 'qrcode': None})
        if not isinstance(qr, str) or len(qr) > 1024 * 1024:
            raise ValidationError('QR inválido.')
        if qr.startswith('data:image/'):
            if not re.fullmatch(r'data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=\r\n]+', qr):
                raise ValidationError('Imagem de QR inválida.')
        else:
            base64.b64decode(qr, validate=True)
            qr = 'data:image/png;base64,' + qr
        return JsonResponse({'connected': False, 'qrcode': qr})
    except (ValidationError, ValueError):
        return JsonResponse({'message': 'Não foi possível carregar o QR Code. Tente novamente ou avise a equipe responsável.'}, status=502)


@login_required
@never_cache
@require_POST
def finish_coexistence(request, account_id, connection_id):
    account, _ = access(request, account_id, ['manager'])
    conn = get_object_or_404(account.connections, pk=connection_id, provider='omni', mode='coexistence', active=True)
    try:
        if request.content_type != 'application/json':
            raise ValueError
        data = json.loads(request.body)
        if not isinstance(data, dict) or data.get('coexistence') is not True:
            raise ValueError
        if any(not isinstance(data.get(k), str) or not data[k] or not data[k].isdigit() or len(data[k]) > 80 for k in ('wabaId', 'phoneId')):
            raise ValueError
        if not isinstance(data.get('code'), str) or not 1 <= len(data['code']) <= 8000:
            raise ValueError
        if not omni_secret(conn) or not conn.webhook_id or conn.instance_id.startswith('pending_'):
            raise ValueError
        # Never disconnect an existing number or silently accept Cloud API mode.
        if conn.setup_status == 'connected':
            return JsonResponse({'message': 'Esta conexão já foi concluída.'}, status=409)
        result = omni_call(conn.instance_id, omni_secret(conn), '/connect',
            {k: data[k] for k in ('wabaId', 'phoneId', 'code', 'coexistence')}, 'POST')
        if result.get('success') is not True:
            raise ValidationError('Provedor não confirmou a conexão.')
        conn.setup_status = 'connected'
        conn.save(update_fields=['setup_status'])
        Audit.objects.create(account=account, actor=request.user, action='whatsapp.connected', object_id=str(conn.id), detail={'mode': 'coexistence'})
        return JsonResponse({'connected': True})
    except (ValueError, TypeError, UnicodeDecodeError):
        return JsonResponse({'message': 'Escolha Coexistência para manter o número no WhatsApp Business e conclua a autorização.'}, status=400)
    except ValidationError:
        return JsonResponse({'message': 'Não foi possível confirmar a conexão. Não repita a autorização sem conferir o estado com a equipe responsável.'}, status=502)


@login_required
@require_POST
def retry_setup(request, account_id, connection_id):
    if not request.user.is_superuser:
        raise PermissionDenied
    account, _ = access(request, account_id, ['manager'])
    conn = get_object_or_404(account.connections, pk=connection_id, provider='omni')
    try:
        if conn.instance_id.startswith('pending_'):
            raise ValidationError('Confira a tentativa no provedor e vincule o canal pela configuração técnica. Não será criado outro canal automaticamente.')
        register_omni(conn, settings.PUBLIC_BASE_URL, request.user)
        conn.setup_status = 'pending'
        conn.save(update_fields=['setup_status'])
        messages.success(request, 'Conexão preparada. Agora prossiga com o número.')
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
    return redirect('whatsapp_connect', account_id=account.id, connection_id=conn.id)
