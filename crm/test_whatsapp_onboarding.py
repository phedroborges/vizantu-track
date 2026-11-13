import hashlib
import hmac
import json
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import override_settings, Client
from django.urls import reverse
from django.utils import timezone

from .forms import AccountForm
from .integrations import seal, unseal
from .models import Connection, OmniWorkspace
from .tests import TenantFixture


@override_settings(PUBLIC_BASE_URL='https://track.example.com')
class WhatsAppOnboardingTests(TenantFixture):
    def setUp(self):
        OmniWorkspace.objects.create(credentials=seal({'secret_key': 'private-workspace-key', 'public_key': 'sdk-public-key'}))
        self.login()

    def prepare(self, mode='qr'):
        self.conn.provider = 'omni'
        self.conn.mode = mode
        self.conn.webhook_id = 'our-hook'
        self.conn.credentials = seal({'signing_secret': 'signature-key'})
        self.conn.save()
        return self.conn

    def test_customer_screen_offers_two_modes_without_technical_fields(self):
        response = self.client.get(reverse('connections', args=[self.account.id]))
        self.assertContains(response, 'Escanear QR Code')
        self.assertContains(response, 'Usar Coexistência')
        for text in ('Secret Key', 'ID da instância', 'name="provider"', 'private-workspace-key', 'Diagnóstico técnico'):
            self.assertNotContains(response, text)
        self.assertEqual(self.client.get(reverse('whatsapp_workspace')).status_code, 403)
        self.assertEqual(self.client.get(reverse('connection_technical', args=[self.account.id])).status_code, 403)

    @patch('crm.whatsapp_views.api_call')
    @patch('crm.integrations.omni_call')
    def test_new_channel_is_created_once_and_webhook_prepared_before_qr(self, webhook, create):
        create.return_value = {'id': 'CHANNEL_NEW', 'token': 'private-channel-token'}
        webhook.return_value = {'id': 'hook-id', 'secret': 'signing-key'}
        data = {'request_id': str(uuid.uuid4()), 'name': 'Atendimento', 'mode': 'qr'}
        url = reverse('connections', args=[self.account.id])
        for _ in range(2):
            self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(webhook.call_count, 1)
        conn = Connection.objects.get(pk=data['request_id'])
        self.assertEqual(conn.account, self.account)
        self.assertEqual(conn.webhook_id, 'hook-id')
        self.assertEqual(conn.phone, '')
        self.assertEqual(create.call_args.args[2]['type'], 'Z_API_WHATSAPP')
        self.assertEqual(webhook.call_args.args[3]['payloadFormat'], 'Z_API')
        self.assertEqual(unseal(conn.credentials)['signing_secret'], 'signing-key')

    @patch('crm.whatsapp_views.api_call')
    def test_missing_keys_prevents_channel_creation_and_explains_setup(self, create):
        OmniWorkspace.objects.all().delete()
        response = self.client.post(reverse('connections', args=[self.account.id]), {
            'request_id': str(uuid.uuid4()), 'name': 'Principal', 'mode': 'qr'})
        self.assertContains(response, 'aguarda liberação')
        create.assert_not_called()

    @patch('crm.whatsapp_views.api_call')
    def test_failed_creation_is_not_blindly_repeated(self, create):
        create.side_effect = ValidationError('Provider unavailable')
        data = {'request_id': str(uuid.uuid4()), 'name': 'Atendimento', 'mode': 'qr'}
        for _ in range(2):
            self.client.post(reverse('connections', args=[self.account.id]), data)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(Connection.objects.get(pk=data['request_id']).setup_status, 'error')

    @patch('crm.whatsapp_views.omni_call')
    def test_qr_is_scoped_private_and_connected_state_is_verified(self, call):
        self.prepare()
        url = reverse('whatsapp_qr', args=[self.account.id, self.conn.id])
        call.return_value = {'connected': False, 'qrcode': 'aW1hZ2U='}
        response = self.client.post(url)
        self.assertEqual(response.json()['qrcode'], 'data:image/png;base64,aW1hZ2U=')
        self.assertEqual(response.headers['Cache-Control'].find('no-store') >= 0, True)
        self.assertNotContains(response, 'private-workspace-key')
        call.return_value = {'connected': True}
        self.assertTrue(self.client.post(url).json()['connected'])
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.setup_status, 'connected')
        self.login(self.viewer)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.login(self.operator)
        self.assertEqual(self.client.post(reverse('whatsapp_qr', args=[self.other.id, self.conn.id])).status_code, 404)

    @patch('crm.whatsapp_views.omni_call')
    def test_qr_does_not_return_arbitrary_remote_images_or_svg(self, call):
        self.prepare()
        for qr in ('https://evil.example/qr', 'data:image/svg+xml;base64,AAAA'):
            call.return_value = {'connected': False, 'qrcode': qr}
            self.assertEqual(self.client.post(reverse('whatsapp_qr', args=[self.account.id, self.conn.id])).status_code, 502)

    @patch('crm.whatsapp_views.omni_call')
    def test_coexistence_requires_explicit_mode_and_server_confirmation(self, call):
        self.prepare('coexistence')
        url = reverse('whatsapp_finish', args=[self.account.id, self.conn.id])
        data = {'wabaId': '123', 'phoneId': '456', 'code': 'secret-code', 'coexistence': False}
        self.assertEqual(self.client.post(url, data=json.dumps(data), content_type='application/json').status_code, 400)
        call.assert_not_called()
        data['coexistence'] = True
        call.return_value = {'success': True}
        self.assertTrue(self.client.post(url, data=json.dumps(data), content_type='application/json').json()['connected'])
        self.assertEqual(self.client.post(url, data=json.dumps(data), content_type='application/json').status_code, 409)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[2], '/connect')

    def test_new_phone_and_default_link_are_learned_only_from_signed_event(self):
        self.prepare()
        self.conn.phone = ''
        self.conn.save(update_fields=['phone'])
        payload = self.payload(connectedPhone='5511999999999')
        raw = json.dumps(payload).encode()
        stamp = str(int(timezone.now().timestamp()))
        digest = hmac.new(b'signature-key', f'{stamp}\nevent\n0\n1\n'.encode() + raw, hashlib.sha256).hexdigest()
        url = reverse('omni_webhook', args=[self.conn.id, self.token])
        self.assertEqual(self.client.post(url, data=raw, content_type='application/json').status_code, 401)
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.phone, '')
        response = self.client.post(url, data=raw, content_type='application/json',
            HTTP_AUTHORIZATION='Bearer ' + self.token, HTTP_X_WEBHOOK_SIGNATURE=f't={stamp},v1={digest}', HTTP_X_IDEMPOTENCY_KEY='event:0:1')
        self.assertEqual(response.status_code, 200)
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.phone, '5511999999999')
        self.assertEqual(self.conn.links.get(name='Link principal').source, 'link')

    def test_admin_workspace_settings_keep_secrets_out_of_html(self):
        admin = get_user_model().objects.create_superuser('owner', password='test-password-123')
        self.login(admin)
        response = self.client.get(reverse('whatsapp_workspace'))
        self.assertContains(response, 'sdk-public-key')
        self.assertNotContains(response, 'private-workspace-key')
        self.assertEqual(self.client.get(reverse('connection_technical', args=[self.account.id])).status_code, 200)

    def test_new_client_identifier_is_internal_and_links_use_public_origin(self):
        form = AccountForm({'name': 'Casa Caramelo #1'})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.cleaned_data['integration_key'].startswith('CLIENTE_'))
        response = self.client.get(reverse('links', args=[self.account.id]))
        self.assertTrue(response.context['links'][0].url.startswith('https://track.example.com/r/'))
        self.assertContains(response, 'Funciona sem anúncios')

    def test_projects_show_setup_progress(self):
        response = self.client.get(reverse('home'))
        self.assertContains(response, 'Clientes e projetos')
        self.assertContains(response, 'Em configuração')
        self.assertContains(response, 'Meta opcional')
