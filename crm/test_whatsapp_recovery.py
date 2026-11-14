import io
import time
from unittest.mock import patch
from urllib.error import HTTPError

from django.contrib.auth import get_user_model
from django.urls import reverse

from .integrations import ProviderError, api_call, seal, unseal
from .tests import TenantFixture


class RecoveryTests(TenantFixture):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('recovery-owner', password='test-password-123')
        self.client.force_login(self.admin)
        self.conn.provider = 'omni'
        self.conn.instance_id = 'pending_' + self.conn.id.hex
        self.conn.setup_status = 'error'
        self.conn.credentials = seal({'secret_key': 'workspace-test-key'})
        self.conn.save()
        self.url = reverse('whatsapp_retry', args=[self.account.id, self.conn.id])

    @patch('crm.whatsapp_views.api_call')
    def test_creation_requires_confirmation_of_absence(self, create):
        self.client.post(self.url)
        create.assert_not_called()
        self.conn.refresh_from_db()
        self.assertTrue(self.conn.instance_id.startswith('pending_'))

    @patch('crm.whatsapp_views.api_call')
    def test_provider_rejection_is_saved_and_visible_to_admin(self, create):
        create.side_effect = ProviderError('Omni exige perfil ENTERPRISE para criação.', 422)
        self.client.post(self.url, {'confirm_absent': 'yes'})
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.setup_status, 'error')
        self.assertIn('ENTERPRISE', unseal(self.conn.credentials)['setup_error'])
        response = self.client.get(reverse('whatsapp_connect', args=[self.account.id, self.conn.id]))
        self.assertContains(response, 'ENTERPRISE')
        self.assertContains(response, 'ID do canal existente')
        self.assertNotContains(response, 'workspace-test-key')

    @patch('crm.whatsapp_views.register_omni')
    @patch('crm.whatsapp_views.omni_call')
    @patch('crm.whatsapp_views.api_call')
    def test_existing_channel_is_verified_and_bound_without_creating_another(self, create, verify, register):
        verify.return_value = {'connected': False, 'qrcode': None}
        self.client.post(self.url, {'channel_id': 'EXISTING_OMNI'})
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.instance_id, 'EXISTING_OMNI')
        self.assertEqual(self.conn.setup_status, 'pending')
        verify.assert_called_once_with('EXISTING_OMNI', 'workspace-test-key', '/zapi/qr-code/image')
        register.assert_called_once()
        create.assert_not_called()

    @patch('crm.whatsapp_views.omni_call')
    def test_channel_already_owned_by_another_connection_is_not_reassigned(self, verify):
        self.client.post(self.url, {'channel_id': self.other_conn.instance_id})
        verify.assert_not_called()
        self.conn.refresh_from_db()
        self.assertTrue(self.conn.instance_id.startswith('pending_'))

    @patch('crm.whatsapp_views.api_call')
    def test_inflight_creation_is_not_repeated(self, create):
        self.conn.setup_status = 'creating'
        self.conn.credentials = seal({'setup_started_at': time.time()})
        self.conn.save()
        self.client.post(self.url, {'confirm_absent': 'yes'})
        create.assert_not_called()

    @patch('crm.integrations.build_opener')
    def test_enterprise_error_is_sanitized_and_keeps_http_status(self, opener):
        opener.return_value.open.side_effect = HTTPError('https://api.omni.z-api.io', 422, 'rejected', {},
            io.BytesIO(b'{"error":"Channel creation requires ENTERPRISE role","token":"private-secret"}'))
        with self.assertRaises(ProviderError) as raised:
            api_call('https://api.omni.z-api.io/v1/channels', 'private-secret', {}, 'POST')
        self.assertEqual(raised.exception.http_status, 422)
        self.assertIn('ENTERPRISE', str(raised.exception))
        self.assertNotIn('private-secret', str(raised.exception))
