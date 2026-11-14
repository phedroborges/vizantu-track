import base64
import hashlib
import json
import re
import secrets
import time
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction, IntegrityError
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
                        'credentials': seal({'setup_started_at': time.time()}),
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
                    except ValidationError as exc:
                        values = unseal(conn.credentials)
                        values['setup_error'] = '; '.join(exc.messages)[:400]
                        code = getattr(exc, 'http_status', None)
                        values['creation_rejected'] = bool(conn.instance_id.startswith('pending_') and code in (400, 401, 403, 422))
                        conn.credentials = seal(values)
                        conn.setup_status = 'error'
                        conn.save(update_fields=['setup_status', 'credentials'])
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
    values = unseal(conn.credentials)
    return page(request, 'crm/whatsapp_connect.html', account, role, connection=conn, ready=ready,
        setup_error=values.get('setup_error', ''), creation_rejected=values.get('creation_rejected', False),
        pending_channel=conn.instance_id.startswith('pending_'),
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
    values = unseal(conn.credentials)
    if conn.setup_status == 'creating' and time.time() - values.get('setup_started_at', 0) < 120:
        messages.error(request, 'Esta preparação já está em andamento. Aguarde antes de tentar novamente.')
        return redirect('whatsapp_connect', account_id=account.id, connection_id=conn.id)
    old_credentials = conn.credentials
    values['setup_started_at'] = time.time()
    conn.credentials = seal(values)
    if not Connection.objects.filter(pk=conn.pk, credentials=old_credentials, setup_status=conn.setup_status).update(setup_status='creating', credentials=conn.credentials):
        messages.error(request, 'A conexão foi atualizada por outra tentativa. Atualize a página.')
        return redirect('whatsapp_connect', account_id=account.id, connection_id=conn.id)
    try:
        supplied = request.POST.get('channel_id', '').strip()
        if supplied:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', supplied) or supplied.startswith('pending_'):
                raise ValidationError('Informe o ID real do canal mostrado no painel Omni.')
            if Connection.objects.filter(instance_id=supplied).exclude(pk=conn.pk).exists():
                raise ValidationError('Este canal já está vinculado a outra conexão no Rastro.')
            if conn.last_received_at or not conn.instance_id.startswith('pending_'):
                raise ValidationError('Esta conexão já possui um canal; ele não será substituído.')
            if conn.mode != 'qr':
                raise ValidationError('O vínculo manual nesta tela atende canais WhatsApp Web por QR Code.')
            omni_call(supplied, omni_secret(conn), '/zapi/qr-code/image')
            conn.instance_id = supplied
            conn.save(update_fields=['instance_id'])
            Audit.objects.create(account=account, actor=request.user, action='omni.channel_bound', object_id=str(conn.pk))
        if conn.instance_id.startswith('pending_'):
            values = unseal(conn.credentials)
            if request.POST.get('confirm_absent') != 'yes':
                raise ValidationError('Confira a lista de conexões do Omni. Se o canal existir, informe o ID abaixo. Se não existir, confirme a ausência para tentar a criação.')
            result = api_call('https://api.omni.z-api.io/v1/channels', omni_secret(conn),
                {'name': f'{account.name} · {conn.name} · {str(conn.id)[:8]}',
                 'type': 'META_WHATSAPP' if conn.mode == 'coexistence' else 'Z_API_WHATSAPP'}, 'POST')
            channel_id = result.get('id')
            if not isinstance(channel_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', channel_id):
                raise ValidationError('Resposta sem ID válido. Confira a lista no Omni antes de tentar novamente.')
            conn.instance_id = channel_id
            values['channel_token'] = result.get('token', '')
            values.pop('setup_error', None)
            conn.credentials = seal(values)
            conn.save(update_fields=['instance_id', 'credentials'])
        register_omni(conn, settings.PUBLIC_BASE_URL, request.user)
        values = unseal(conn.credentials)
        values.pop('setup_error', None)
        conn.credentials = seal(values)
        conn.setup_status = 'pending'
        conn.save(update_fields=['setup_status', 'credentials'])
        messages.success(request, 'Conexão preparada. Agora prossiga com o número.')
    except (ValidationError, IntegrityError) as exc:
        values = unseal(conn.credentials)
        error_text = '; '.join(exc.messages) if isinstance(exc, ValidationError) else 'Este canal já foi vinculado por outra tentativa. Confira as conexões existentes.'
        values['setup_error'] = error_text[:400]
        conn.credentials = seal(values)
        conn.setup_status = 'error'
        conn.save(update_fields=['credentials', 'setup_status'])
        messages.error(request, error_text)
    return redirect('whatsapp_connect', account_id=account.id, connection_id=conn.id)
