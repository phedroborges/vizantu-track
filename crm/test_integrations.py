import gzip
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from .integrations import (ad_accounts, graph_pages, register_omni, request_sync,
                           seal, sync_ads_once, unseal, verify_omni)
from .models import AdInsight, Connection, MetaIntegration, WebhookEvent
from .services import process_inbox_once, rotate_webhook
from .tests import TenantFixture


class IntegrationTests(TenantFixture):
    def setUp(self):
        self.integration = MetaIntegration.objects.create(account=self.account,
            credentials=seal({'app_id': '1234', 'app_secret': 'secret-value', 'access_token': 'private-token'}))

    def omni(self):
        self.conn.provider = 'omni'
        self.conn.credentials = seal({'secret_key': 'omni-secret', 'signing_secret': 'sign-secret', 'webhook_token': self.token})
        self.conn.save()
        return reverse('omni_webhook', args=[self.conn.id, self.token])

    def signed_post(self, payload, compressed=False, change=None, **headers):
        raw = json.dumps(payload).encode()
        stamp = str(int(timezone.now().timestamp()))
        signature = hmac.new(b'sign-secret', f'{stamp}\nevent\n0\n5\n'.encode() + raw, hashlib.sha256).hexdigest()
        params = {'HTTP_AUTHORIZATION': 'Bearer ' + self.token,
                  'HTTP_X_WEBHOOK_SIGNATURE': f't={stamp},v1={signature}', 'HTTP_X_IDEMPOTENCY_KEY': 'event:0:5'}
        params.update(headers)
        if compressed:
            params['HTTP_CONTENT_ENCODING'] = 'gzip'
            raw = gzip.compress(raw)
        return self.client.post(reverse('omni_webhook', args=[self.conn.id, self.token]),
            data=change or raw, content_type='application/json', **params)

    def test_secrets_are_encrypted_and_wrong_key_fails(self):
        self.assertNotIn('private-token', self.integration.credentials)
        self.assertEqual(unseal(self.integration.credentials)['access_token'], 'private-token')
        with override_settings(SECRET_KEY='another-secret-key-012345678901234567890'):
            with self.assertRaises(ValidationError):
                unseal(self.integration.credentials)

    def test_signed_omni_gzip_dedup_and_worker(self):
        self.omni()
        payload = self.payload(externalAdReply={'sourceType': 'ad', 'sourceId': '987', 'ctwaClid': 'click-id'})
        self.assertEqual(self.signed_post(payload, compressed=True).status_code, 200)
        self.assertEqual(self.signed_post(payload).json()['duplicate'], True)
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertTrue(process_inbox_once())
        self.assertEqual(self.conn.leads.get().attributions.get().ctwa_clid, 'click-id')

    def test_omni_rejects_signature_auth_channel_and_format(self):
        self.omni()
        self.assertEqual(self.signed_post(self.payload(), HTTP_AUTHORIZATION='Bearer wrong').status_code, 401)
        self.assertEqual(self.signed_post(self.payload(), change=b'{}').status_code, 401)
        self.assertEqual(self.signed_post(self.payload(instanceId='OTHER')).status_code, 400)
        self.assertEqual(self.signed_post({'message': {}}).status_code, 400)
        self.assertEqual(self.client.post(self.webhook_url(), data=json.dumps(self.payload()), content_type='application/json').status_code, 404)
        self.assertFalse(WebhookEvent.objects.exists())

    def test_omni_rejects_stale_and_malformed_signature(self):
        raw = b'{}'
        stamp = str(int(timezone.now().timestamp()) - 301)
        digest = hmac.new(b's', f'{stamp}\nevent\n0\n5\n'.encode() + raw, hashlib.sha256).hexdigest()
        self.assertFalse(verify_omni(raw, f't={stamp},v1={digest}', 'event:0:5', 's'))
        self.assertFalse(verify_omni(raw, 'broken', 'broken', 's'))

    def test_omni_gzip_expansion_is_bounded(self):
        self.omni()
        payload = self.payload(text={'message': 'x' * (512 * 1024)})
        self.assertEqual(self.signed_post(payload, compressed=True).status_code, 413)

    def test_invalid_gzip_and_non_ascii_auth_are_rejected(self):
        self.omni()
        self.assertEqual(self.signed_post(self.payload(), change=b'bad gzip', HTTP_CONTENT_ENCODING='gzip').status_code, 400)
        self.assertEqual(self.signed_post(self.payload(), HTTP_AUTHORIZATION='Bearer inválido').status_code, 401)

    @patch('crm.integrations.omni_call')
    def test_registration_updates_only_owned_webhook_in_compatible_format(self, call):
        self.omni()
        call.return_value = {'id': 'our-webhook', 'secret': 'signed-secret'}
        url = register_omni(self.conn, 'https://track.example.com', self.manager)
        self.assertIn('/hooks/omni/', url)
        args = call.call_args.args
        self.assertEqual(args[2], '/webhooks')
        self.assertEqual(args[3]['payloadFormat'], 'Z_API')
        self.assertEqual(args[3]['events'], ['RECEIVED_AND_DELIVERY'])
        self.assertTrue(args[3]['signing'])
        self.assertEqual(args[4], 'POST')
        register_omni(self.conn, 'https://track.example.com', self.manager)
        self.assertEqual(call.call_args.args[2], '/webhooks/our-webhook')
        self.assertEqual(call.call_args.args[4], 'PATCH')

    def test_meta_settings_does_not_echo_tokens_and_is_manager_only(self):
        self.login()
        response = self.client.get(reverse('meta_settings', args=[self.account.id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'private-token')
        self.assertNotContains(response, 'secret-value')
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse('meta_settings', args=[self.account.id])).status_code, 403)
        self.assertEqual(self.client.get(reverse('ads_report', args=[self.account.id])).status_code, 200)
        self.login(self.operator)
        self.assertEqual(self.client.get(reverse('meta_settings', args=[self.other.id])).status_code, 200)
        self.assertEqual(self.client.post(reverse('meta_settings', args=[self.account.id]), {'action': 'sync'}).status_code, 403)

    @override_settings(META_REDIRECT_URI='')
    @patch('crm.meta_views.graph')
    def test_oauth_bound_to_session_account_and_single_use(self, graph):
        self.login()
        response = self.client.post(reverse('meta_connect', args=[self.account.id]))
        self.assertEqual(response.status_code, 302)
        pending = self.client.session['meta_oauth']
        self.assertIn('ads_read', response.url)
        self.assertNotIn('secret-value', response.url)
        graph.return_value = {'access_token': 'new-token', 'expires_in': 3600}
        callback = reverse('meta_callback')
        response = self.client.get(callback, {'state': pending['state'], 'code': 'valid'})
        self.assertEqual(response.status_code, 302)
        self.integration.refresh_from_db()
        self.assertEqual(unseal(self.integration.credentials)['access_token'], 'new-token')
        self.assertIsNotNone(self.integration.token_expires_at)
        self.assertEqual(self.client.get(callback, {'state': pending['state'], 'code': 'valid'}).status_code, 400)
        self.assertEqual(graph.call_count, 1)

    @patch('crm.meta_views.graph')
    def test_oauth_rejects_bad_state_and_expired_state_without_network(self, graph):
        self.login()
        self.client.post(reverse('meta_connect', args=[self.account.id]))
        self.assertEqual(self.client.get(reverse('meta_callback'), {'state': 'wrong', 'code': 'x'}).status_code, 400)
        self.client.post(reverse('meta_connect', args=[self.account.id]))
        session = self.client.session
        pending = session['meta_oauth']
        pending['at'] -= 601
        session['meta_oauth'] = pending
        session.save()
        self.assertEqual(self.client.get(reverse('meta_callback'), {'state': pending['state'], 'code': 'x'}).status_code, 400)
        graph.assert_not_called()

    @patch('crm.integrations.graph')
    def test_pagination_reconstructs_safe_url_and_requires_complete_response(self, graph):
        graph.side_effect = [{'data': [{'id': '1'}], 'paging': {'next': 'https://evil.example/?token=leak', 'cursors': {'after': 'cursor'}}}, {'data': [{'id': '2'}]}]
        rows = ad_accounts(self.integration)
        self.assertEqual(len(rows), 2)
        self.assertEqual(graph.call_args.args[0], 'me/adaccounts')
        self.assertEqual(graph.call_args.args[2]['after'], 'cursor')
        graph.side_effect = None
        graph.return_value = {'data': [], 'paging': {'next': 'somewhere'}}
        with self.assertRaises(ValidationError):
            ad_accounts(self.integration)

    def insight(self, **changes):
        data = {'account_id': '99', 'ad_id': '987', 'ad_name': 'Anúncio A', 'adset_id': '80',
                'adset_name': 'Conjunto', 'campaign_id': '70', 'campaign_name': 'Campanha',
                'spend': '12.50', 'impressions': '100', 'clicks': '9',
                'date_start': timezone.localdate().isoformat(), 'date_stop': timezone.localdate().isoformat()}
        return dict(data, **changes)

    def configured(self):
        self.integration.ad_account_id = 'act_99'
        self.integration.currency = 'BRL'
        self.integration.save()

    @patch('crm.integrations.graph_pages')
    def test_sync_upserts_daily_metrics_and_links_report_to_received_ad_id(self, pages):
        self.configured()
        pages.return_value = [self.insight()]
        request_sync(self.integration)
        with self.assertRaises(ValidationError):
            request_sync(self.integration)
        self.assertTrue(sync_ads_once())
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.sync_status, 'done')
        self.assertEqual(AdInsight.objects.get().spend, 12.5)
        request_sync(self.integration)
        sync_ads_once()
        self.assertEqual(AdInsight.objects.count(), 1)
        from .services import ingest_message
        ingest_message(self.conn, self.payload(externalAdReply={'sourceType': 'ad', 'sourceId': '987'}))
        self.login()
        response = self.client.get(reverse('ads_report', args=[self.account.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['rows'][0]['leads'], 1)
        self.assertEqual(response.context['rows'][0]['cost_per_lead'], 12.5)

    @patch('crm.integrations.graph_pages')
    def test_incomplete_or_other_account_response_preserves_previous_insights(self, pages):
        self.configured()
        pages.return_value = [self.insight()]
        request_sync(self.integration)
        sync_ads_once()
        for bad in [self.insight(account_id='other'), self.insight(spend='NaN'), self.insight(date_start='2000-01-01')]:
            pages.return_value = [bad]
            request_sync(self.integration)
            sync_ads_once()
            self.assertEqual(AdInsight.objects.count(), 1)
            self.integration.refresh_from_db()
            self.assertEqual(self.integration.sync_status, 'failed')

    @patch('crm.meta_views.ad_accounts')
    def test_account_selection_revalidates_ownership(self, accounts):
        self.login()
        accounts.return_value = [{'id': 'act_99', 'name': 'Minha conta', 'currency': 'BRL'}]
        url = reverse('meta_settings', args=[self.account.id])
        self.client.post(url, {'action': 'select', 'ad_account_id': 'act_notmine'})
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.ad_account_id, '')
        self.client.post(url, {'action': 'select', 'ad_account_id': 'act_99'})
        self.integration.refresh_from_db()
        self.assertEqual(self.integration.ad_account_id, 'act_99')
        self.assertEqual(self.integration.sync_status, 'pending')

    @patch('crm.integrations.graph_pages')
    def test_renamed_ad_is_one_report_row_and_hourly_sync_runs(self, pages):
        self.configured()
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        pages.return_value = [self.insight(ad_name='Nome novo'), self.insight(ad_name='Nome antigo', date_start=yesterday, date_stop=yesterday)]
        request_sync(self.integration)
        sync_ads_once()
        self.login()
        response = self.client.get(reverse('ads_report', args=[self.account.id]))
        self.assertEqual(len(response.context['rows']), 1)
        self.assertEqual(response.context['rows'][0]['ad_name'], 'Nome novo')
        self.assertEqual(response.context['rows'][0]['spend'], 25)
        self.assertFalse(sync_ads_once())
        MetaIntegration.objects.filter(pk=self.integration.pk).update(last_synced_at=timezone.now() - timedelta(hours=2))
        self.assertTrue(sync_ads_once())
