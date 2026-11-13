import hashlib
import json
import os
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (Attribution, Audit, Click, Connection, Dispatch, Lead, Membership,
                     Message, Sale, TrackableLink, WebhookEvent)
from .services import (change_stage, create_account, dispatch_once, ingest_message,
                       process_inbox_once, queue_conversion, record_sale, rotate_webhook)
from .views import csv_safe


class TenantFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = get_user_model().objects.create_user('manager', password='test-password-123')
        cls.viewer = get_user_model().objects.create_user('viewer', password='test-password-123')
        cls.operator = get_user_model().objects.create_user('operator', password='test-password-123')
        cls.account = create_account('Cliente A', 'CLIENTE_A', cls.manager)
        cls.other = create_account('Cliente B', 'CLIENTE_B', cls.operator)
        Membership.objects.create(account=cls.account, user=cls.viewer, role='viewer')
        Membership.objects.create(account=cls.account, user=cls.operator, role='operator')
        cls.conn = Connection.objects.create(account=cls.account, name='Comercial', instance_id='INSTANCE_A', phone='5511999999999')
        cls.token = rotate_webhook(cls.conn, cls.manager)
        cls.other_conn = Connection.objects.create(account=cls.other, name='Outro', instance_id='INSTANCE_B', phone='5511988888888')
        rotate_webhook(cls.other_conn, cls.operator)
        cls.link = TrackableLink.objects.create(connection=cls.conn, name='Campanha', source='google', medium='cpc', campaign='Pesquisa')

    def payload(self, **changes):
        body = {'instanceId': self.conn.instance_id, 'connectedPhone': self.conn.phone, 'type': 'ReceivedCallback',
                'phone': '5511977777777', 'messageId': 'msg-one', 'fromMe': False, 'isGroup': False,
                'momment': int(timezone.now().timestamp() * 1000), 'chatName': 'Maria', 'text': {'message': 'Olá!'}}
        body.update(changes)
        return body

    def lead(self):
        ingest_message(self.conn, self.payload())
        return self.conn.leads.get()

    def webhook_url(self, token=None):
        return reverse('zapi_webhook', args=[self.conn.id, token or self.token])

    def login(self, user=None):
        self.client.force_login(user or self.manager)


class CoreTests(TenantFixture):
    def test_tenant_cannot_read_dashboard_lead_or_csv(self):
        lead = self.lead()
        self.client.force_login(self.operator)
        Membership.objects.filter(account=self.account, user=self.operator).delete()
        for route, args in [('dashboard', [self.account.id]), ('leads', [self.account.id]),
                            ('export_csv', [self.account.id]), ('lead_detail', [self.account.id, lead.id]),
                            ('links', [self.account.id]), ('connections', [self.account.id])]:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route, args=args)).status_code, 404)

    def test_viewer_cannot_write_sale_stage_link_or_settings(self):
        lead = self.lead()
        self.login(self.viewer)
        for route, args, data in [('lead_detail', [self.account.id, lead.id], {'action': 'sale', 'amount': '10'}),
                                  ('lead_detail', [self.account.id, lead.id], {'action': 'stage', 'stage': lead.stage_id}),
                                  ('links', [self.account.id], {}), ('connections', [self.account.id], {}),
                                  ('account_settings', [self.account.id], {})]:
            with self.subTest(route=route):
                self.assertEqual(self.client.post(reverse(route, args=args), data).status_code, 403)
        self.assertEqual(Sale.objects.count(), 0)

    def test_operator_cannot_manage_integrations_or_send_conversions(self):
        lead = self.lead()
        self.login(self.operator)
        for route in ('connections', 'account_settings'):
            self.assertEqual(self.client.get(reverse(route, args=[self.account.id])).status_code, 403)
        self.assertEqual(self.client.post(reverse('lead_detail', args=[self.account.id, lead.id]), {'action': 'conversion'}).status_code, 403)

    def test_cross_tenant_stage_refused(self):
        lead = self.lead()
        with self.assertRaises(ValidationError):
            change_stage(lead, self.other.stages.first(), self.manager)
        lead.refresh_from_db()
        self.assertEqual(lead.stage.account_id, self.account.id)

    def test_cross_tenant_connection_refused_in_link_form(self):
        self.login()
        response = self.client.post(reverse('links', args=[self.account.id]), {
            'connection': str(self.other_conn.id), 'name': 'Bad', 'text': 'Oi', 'source': 'site'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TrackableLink.objects.count(), 1)
        self.assertContains(response, 'válida')

    def test_webhook_requires_secret_and_correct_instance(self):
        body = self.payload()
        self.assertEqual(self.client.post(self.webhook_url('wrong'), body, content_type='application/json').status_code, 401)
        body['instanceId'] = 'INSTANCE_B'
        self.assertEqual(self.client.post(self.webhook_url(), body, content_type='application/json').status_code, 400)
        body['instanceId'], body['connectedPhone'] = self.conn.instance_id, self.other_conn.phone
        self.assertEqual(self.client.post(self.webhook_url(), body, content_type='application/json').status_code, 400)
        self.assertEqual(WebhookEvent.objects.count(), 0)

    def test_durable_inbox_and_replayed_payload(self):
        body = self.payload()
        for _ in range(2):
            self.assertEqual(self.client.post(self.webhook_url(), body, content_type='application/json').status_code, 200)
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 0)
        self.assertTrue(process_inbox_once())
        self.assertFalse(process_inbox_once())
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(WebhookEvent.objects.get().status, 'done')

    def test_different_payload_same_message_is_idempotent(self):
        body = self.payload()
        ingest_message(self.conn, body)
        body['chatName'] = 'Nome alterado'
        ingest_message(self.conn, body)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(Lead.objects.count(), 1)

    def test_out_of_order_and_outgoing_messages(self):
        now = timezone.now()
        ingest_message(self.conn, self.payload(momment=int(now.timestamp() * 1000)))
        earlier = now - timedelta(days=2)
        ingest_message(self.conn, self.payload(messageId='old', momment=int(earlier.timestamp() * 1000)))
        ingest_message(self.conn, self.payload(messageId='out', fromMe=True))
        lead = self.conn.leads.get()
        self.assertAlmostEqual(lead.first_message_at.timestamp(), earlier.timestamp(), places=2)
        self.assertEqual(lead.messages.filter(direction='out').count(), 1)

    def test_edit_updates_without_duplicating(self):
        body = self.payload()
        ingest_message(self.conn, body)
        body.update({'isEdit': True, 'text': {'message': 'Corrigida'}})
        ingest_message(self.conn, body)
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(Message.objects.get().text, 'Corrigida')
        self.assertTrue(Message.objects.get().edited)

    def test_groups_ignored_and_lid_preserved_as_failed(self):
        self.client.post(self.webhook_url(), self.payload(isGroup=True), content_type='application/json')
        process_inbox_once()
        self.assertEqual(WebhookEvent.objects.get().status, 'ignored')
        self.client.post(self.webhook_url(), self.payload(phone='123456789@lid'), content_type='application/json')
        process_inbox_once()
        self.assertEqual(WebhookEvent.objects.filter(status='failed').count(), 1)
        self.assertEqual(Message.objects.count(), 0)

    def test_invalid_direction_or_timestamp_not_coerced(self):
        for changes in ({'fromMe': 'false'}, {'momment': 'bad'}, {'momment': float('inf')}, {'text': {'message': 123}}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, OverflowError, OSError)):
                ingest_message(self.conn, self.payload(**changes))
        self.assertEqual(Lead.objects.count(), 0)

    def test_click_alone_never_creates_lead(self):
        response = self.client.get(reverse('track', args=[self.link.id]), {'utm_campaign': 'Ofertas', 'gclid': 'g123'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Lead.objects.count(), 0)
        click = Click.objects.get()
        self.assertEqual(click.campaign, 'Ofertas')
        self.assertEqual(click.gclid, 'g123')
        text = parse_qs(urlparse(response['Location']).query)['text'][0]
        self.assertIn('[R:' + click.id.hex + ']', text)
        self.assertEqual(response['Cache-Control'], 'no-store, private')

    def test_attribution_only_after_matching_received_code(self):
        self.client.get(reverse('track', args=[self.link.id]), {'gclid': 'g123'})
        click = Click.objects.get()
        body = self.payload(text={'message': 'Oi [R:' + click.id.hex + ']'})
        ingest_message(self.conn, body)
        attr = Attribution.objects.get()
        self.assertEqual(attr.source, 'google')
        self.assertEqual(attr.gclid, 'g123')
        self.assertEqual(attr.evidence, 'message_code')
        click.refresh_from_db()
        self.assertEqual(click.matched_lead_id, attr.lead_id)
        body.update({'phone': '5511966666666', 'messageId': 'another'})
        ingest_message(self.conn, body)
        self.assertEqual(Attribution.objects.count(), 1)

    def test_code_from_other_connection_or_outgoing_not_matched(self):
        click = Click.objects.create(link=self.link, source='google')
        text = {'message': 'Oi [R:' + click.id.hex + ']'}
        ingest_message(self.other_conn, self.payload(text=text))
        ingest_message(self.conn, self.payload(text=text, fromMe=True))
        self.assertEqual(Attribution.objects.count(), 0)

    def test_expired_or_future_code_not_attributed(self):
        for index, at in enumerate((timezone.now() - timedelta(days=31), timezone.now() + timedelta(days=1))):
            click = Click.objects.create(link=self.link, created_at=at)
            ingest_message(self.conn, self.payload(messageId='c' + str(index), text={'message': '[R:' + click.id.hex + ']'}))
        self.assertEqual(Attribution.objects.count(), 0)

    def test_ad_referral_captures_click_id(self):
        ingest_message(self.conn, self.payload(externalAdReply={'sourceType': 'ad', 'sourceId': '123', 'ctwaClid': 'raw-click-id', 'title': 'Anúncio'}))
        attr = Attribution.objects.get()
        self.assertEqual((attr.source, attr.ad_id, attr.ctwa_clid), ('meta', '123', 'raw-click-id'))

    def test_link_code_preserves_ad_click_metadata(self):
        click = Click.objects.create(link=self.link, source='meta')
        ingest_message(self.conn, self.payload(text={'message': '[R:' + click.id.hex + ']'},
            externalAdReply={'sourceType': 'ad', 'sourceId': '789', 'ctwaClid': 'ctwa'}))
        attr = Attribution.objects.get()
        self.assertEqual(attr.evidence, 'message_code')
        self.assertEqual(attr.ctwa_clid, 'ctwa')

    def test_bootstrap_never_changes_existing_user(self):
        with patch.dict(os.environ, {'DJANGO_SUPERUSER_USERNAME': 'viewer', 'DJANGO_SUPERUSER_PASSWORD': 'New-password-long-123'}):
            call_command('bootstrap_admin')
        self.viewer.refresh_from_db()
        self.assertFalse(self.viewer.is_superuser)
        self.assertTrue(self.viewer.check_password('test-password-123'))

    def test_bootstrap_rejects_weak_password(self):
        with patch.dict(os.environ, {'DJANGO_SUPERUSER_USERNAME': 'firstadmin', 'DJANGO_SUPERUSER_PASSWORD': '123'}):
            with self.assertRaises(CommandError):
                call_command('bootstrap_admin')
        self.assertFalse(get_user_model().objects.filter(username='firstadmin').exists())

    def test_sales_positive_decimal_and_idempotency(self):
        lead = self.lead()
        key = uuid.uuid4()
        sale = record_sale(lead, Decimal('123.45'), key, self.manager)
        again = record_sale(lead, Decimal('123.45'), key, self.manager)
        self.assertEqual(sale.pk, again.pk)
        lead.refresh_from_db()
        self.assertTrue(lead.stage.won)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(Dispatch.objects.count(), 0)
        self.assertEqual(Audit.objects.filter(action='sale.created').count(), 1)
        for amount in (Decimal('-1'), Decimal('0'), Decimal('1.001')):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                record_sale(lead, amount, uuid.uuid4(), self.manager)

    def test_sale_idempotency_key_cannot_be_reused_for_different_value(self):
        lead = self.lead()
        key = uuid.uuid4()
        record_sale(lead, Decimal('10'), key, self.manager)
        with self.assertRaises(ValidationError):
            record_sale(lead, Decimal('20'), key, self.manager)

    def test_csrf_rejects_browser_writes(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        self.assertEqual(client.post(reverse('links', args=[self.account.id]), {}).status_code, 403)
        response = client.get(reverse('links', args=[self.account.id]))
        self.assertEqual(response['Referrer-Policy'], 'same-origin')
        token = client.cookies['csrftoken'].value
        self.assertEqual(client.post(reverse('links', args=[self.account.id]),
            {'csrfmiddlewaretoken': token}, HTTP_ORIGIN='http://testserver').status_code, 200)

    def test_login_rate_limit(self):
        for _ in range(20):
            response = self.client.post('/login/', {'username': 'missing', 'password': 'incorrect'})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post('/login/', {'username': 'missing', 'password': 'incorrect'}).status_code, 429)

    def test_all_main_pages_render(self):
        lead = self.lead()
        self.login()
        for route, args in [('home', []), ('dashboard', [self.account.id]), ('leads', [self.account.id]),
                            ('lead_detail', [self.account.id, lead.id]), ('links', [self.account.id]),
                            ('connections', [self.account.id]), ('account_settings', [self.account.id])]:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route, args=args)).status_code, 200)

    def test_csv_filters_and_formula_injection(self):
        lead = self.lead()
        lead.name = '=HYPERLINK("bad")'
        lead.save(update_fields=['name'])
        self.login()
        response = self.client.get(reverse('export_csv', args=[self.account.id]))
        content = b''.join(response.streaming_content).decode('utf-8-sig')
        self.assertIn("'=HYPERLINK", content)
        for text in ('=1+1', '  +SUM(A1)', '@cmd', '\tbad', '-5'):
            self.assertTrue(csv_safe(text).startswith("'"))
        response = self.client.get(reverse('export_csv', args=[self.account.id]), {'q': 'missing'})
        self.assertNotIn('HYPERLINK', b''.join(response.streaming_content).decode())

    def test_rotating_webhook_invalidates_previous_secret(self):
        rotate_webhook(self.conn, self.manager)
        self.assertEqual(self.client.post(self.webhook_url(), self.payload(), content_type='application/json').status_code, 401)

    def test_disabled_connections_cannot_track_or_ingest(self):
        self.conn.active = False
        self.conn.save()
        self.assertEqual(self.client.get(reverse('track', args=[self.link.id])).status_code, 404)
        self.assertEqual(self.client.post(self.webhook_url(), self.payload(), content_type='application/json').status_code, 404)

    def test_invalid_json_rejected_before_queue(self):
        for raw in ('not-json', '[1,2]', 'null'):
            self.assertEqual(self.client.post(self.webhook_url(), raw, content_type='application/json').status_code, 400)
        self.assertEqual(WebhookEvent.objects.count(), 0)


@override_settings(META_GRAPH_VERSION='v25.0')
class ConversionTests(TenantFixture):
    def setUp(self):
        self.env = patch.dict(os.environ, {'META_CLIENTE_A_DATASET_ID': '123', 'META_CLIENTE_A_ACCESS_TOKEN': 'secret',
                                           'META_CLIENTE_A_WABA_ID': '456', 'META_CLIENTE_A_TEST_EVENT_CODE': 'TEST'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def queued(self):
        ingest_message(self.conn, self.payload(externalAdReply={'sourceType': 'ad', 'sourceId': '789', 'ctwaClid': 'ctwa'}))
        sale = record_sale(self.conn.leads.get(), Decimal('99.90'), uuid.uuid4(), self.manager)
        return queue_conversion(sale, self.manager)

    def test_conversion_requires_ctwa_and_never_uses_fbclid(self):
        lead = self.lead()
        sale = record_sale(lead, Decimal('10'), uuid.uuid4(), self.manager)
        with self.assertRaises(ValidationError):
            queue_conversion(sale, self.manager)

    def test_conversion_queued_once_and_payload_stable(self):
        dispatch = self.queued()
        event = dispatch.payload['data'][0]
        self.assertEqual(event['event_id'], str(dispatch.sale_id))
        self.assertEqual(event['user_data']['ctwa_clid'], 'ctwa')
        self.assertEqual(event['custom_data']['value'], 99.9)
        with self.assertRaises(ValidationError):
            queue_conversion(dispatch.sale, self.manager)

    @patch('crm.services.urlopen')
    def test_conversion_confirmed_and_not_sent_twice(self, urlopen):
        dispatch = self.queued()
        urlopen.return_value.__enter__.return_value.read.return_value = b'{"events_received":1}'
        self.assertTrue(dispatch_once())
        self.assertFalse(dispatch_once())
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, 'sent')
        self.assertEqual(urlopen.call_count, 1)
        self.assertNotIn('secret', dispatch.result)

    @patch('crm.services.urlopen', side_effect=URLError('timeout'))
    def test_uncertain_delivery_never_retried_automatically(self, urlopen):
        dispatch = self.queued()
        dispatch_once()
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, 'review')
        self.assertFalse(dispatch_once())
        self.assertEqual(urlopen.call_count, 1)

    @patch('crm.services.urlopen', side_effect=HTTPError('https://graph.facebook.com', 429, 'limit', {}, None))
    def test_rate_limit_retries_later_with_bound(self, urlopen):
        dispatch = self.queued()
        dispatch_once()
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, 'pending')
        self.assertGreater(dispatch.next_attempt_at, timezone.now())
        self.assertFalse(dispatch_once())
        Dispatch.objects.filter(pk=dispatch.pk).update(attempts=4, next_attempt_at=timezone.now())
        dispatch_once()
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, 'failed')

    def test_crashed_worker_puts_dispatch_in_review(self):
        dispatch = self.queued()
        Dispatch.objects.filter(pk=dispatch.pk).update(status='sending', updated_at=timezone.now() - timedelta(minutes=6))
        self.assertFalse(dispatch_once())
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.status, 'review')
