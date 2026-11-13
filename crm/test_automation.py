import json
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError

from django.test import override_settings, Client
from django.urls import reverse
from django.utils import timezone

from .automation import deliver_event_once, enrich_ad_once, prepare_event
from .integrations import seal
from .models import (AutomationConfig, ConversionEvent, MessageRule, MetaIntegration,
                     Sale, Dispatch, Stage)
from .services import ingest_message, change_stage, record_sale
from .tests import TenantFixture


class AutomationTests(TenantFixture):
    def rule(self, target=None, **kwargs):
        return MessageRule.objects.create(account=self.account, name='Confirmação', phrase='obrigado pela compra',
            to_stage=target or self.account.stages.get(won=True), **kwargs)

    def outgoing(self, message='Obrigado pela compra!', **kwargs):
        ingest_message(self.conn, self.payload(messageId=kwargs.pop('messageId', 'outgoing'),
            fromMe=True, text={'message': message}, **kwargs))

    def meta_ready(self, lead, mode='test', events=None):
        cfg = self.account.automation
        cfg.mode = mode
        cfg.allowed_events = events or ['Lead', 'Purchase']
        cfg.save()
        MetaIntegration.objects.create(account=self.account, ad_account_id='act_99', credentials=seal({
            'access_token': 'private-test-token', 'conversion_token': 'private-conversion-token', 'dataset_id': '1234', 'waba_id': '5678', 'test_event_code': 'TEST-CODE'}))
        from .models import Attribution
        message = lead.messages.first()
        Attribution.objects.create(lead=lead, message=message, source='meta', medium='ctwa',
            ad_id='987', ctwa_clid='ctwa-test', occurred_at=message.occurred_at,
            enrichment_status='done', ad_account_id='act_99')

    def test_new_account_has_no_lost_and_initial_contact_is_simulated(self):
        lead = self.lead()
        self.assertEqual(list(self.account.stages.values_list('name', flat=True)), ['Novo contato', 'Qualificado', 'Proposta', 'Venda'])
        event = lead.conversion_events.get()
        self.assertEqual(event.event_name, 'Contact')
        self.assertEqual(event.status, 'simulated')
        self.assertFalse(Dispatch.objects.exists())
        self.assertFalse(deliver_event_once())

    def test_outgoing_phrase_moves_to_sale_without_fabricating_intermediate_events(self):
        lead = self.lead()
        lead.deal_value = Decimal('125.50')
        lead.save(update_fields=['deal_value'])
        self.rule()
        self.outgoing('Muito ÓBRIGADO   pela compra!')
        lead.refresh_from_db()
        self.assertTrue(lead.stage.won)
        sale = Sale.objects.get(lead=lead)
        self.assertEqual(sale.amount, Decimal('125.50'))
        self.assertIsNone(sale.operator)
        self.assertEqual(list(lead.conversion_events.order_by('occurred_at').values_list('event_name', flat=True)), ['Contact', 'Purchase'])
        self.assertEqual(lead.transitions.get(origin='message').snapshot['phrase'], 'obrigado pela compra')
        self.outgoing('obrigado pela compra', messageId='outgoing-two')
        self.assertEqual(Sale.objects.filter(lead=lead).count(), 1)
        self.assertEqual(lead.conversion_events.filter(event_name='Purchase').count(), 1)
        change_stage(lead, self.account.stages.get(name='Proposta'), self.manager)
        self.outgoing('obrigado pela compra', messageId='after-manual-return')
        self.assertEqual(Sale.objects.filter(lead=lead).count(), 1)

    def test_wrong_direction_duplicates_edits_and_old_messages_do_not_trigger(self):
        lead = self.lead()
        self.rule()
        ingest_message(self.conn, self.payload(messageId='customer', text={'message': 'obrigado pela compra'}))
        self.outgoing(isEdit=True)
        self.outgoing(messageId='old', momment=int((timezone.now()-timedelta(days=1)).timestamp()*1000))
        lead.refresh_from_db()
        self.assertFalse(lead.stage.won)
        self.assertFalse(Sale.objects.exists())

    def test_pending_value_is_shown_and_can_be_completed_without_duplicate(self):
        lead = self.lead()
        self.rule()
        self.outgoing()
        event = lead.conversion_events.get(event_name='Purchase')
        self.assertEqual(event.status, 'blocked')
        self.assertIn('valor', event.reason)
        self.assertFalse(Sale.objects.exists())
        self.login()
        self.client.post(reverse('lead_detail', args=[self.account.id, lead.id]), {'action': 'deal_value', 'deal_value': '200.25'})
        event.refresh_from_db()
        self.assertEqual(event.status, 'simulated')
        self.assertEqual(event.sale.amount, Decimal('200.25'))
        self.assertEqual(lead.conversion_events.filter(event_name='Purchase').count(), 1)

    def test_custom_won_stage_event_is_not_emitted_twice_by_sale_registration(self):
        lead = self.lead()
        stage = self.account.stages.get(won=True)
        stage.event_name = 'Lead'
        stage.save()
        record_sale(lead, Decimal('100'), uuid.uuid4(), self.manager)
        self.assertEqual(lead.conversion_events.filter(event_name='Lead').count(), 1)

    def test_fixed_value_exact_matching_priority_and_no_regression(self):
        lead = self.lead()
        self.rule(match_type='exact', fixed_value=Decimal('80'))
        self.outgoing('prefixo obrigado pela compra')
        lead.refresh_from_db()
        self.assertFalse(lead.stage.won)
        self.outgoing('Obrigado pela compra', messageId='exact')
        self.assertEqual(Sale.objects.get().amount, Decimal('80'))
        qualified = self.account.stages.get(name='Qualificado')
        self.rule(target=qualified, priority=1)
        self.outgoing(messageId='regress')
        lead.refresh_from_db()
        self.assertTrue(lead.stage.won)

    @patch('crm.automation.urlopen')
    def test_test_event_has_stable_id_and_no_live_promotion(self, opener):
        lead = self.lead()
        self.meta_ready(lead)
        stage = self.account.stages.get(name='Qualificado')
        change_stage(lead, stage, self.manager)
        event = lead.conversion_events.get(event_name='Lead')
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.payload['test_event_code'], 'TEST-CODE')
        self.assertEqual(event.payload['data'][0]['event_id'], str(event.id))
        response = MagicMock()
        response.read.return_value = b'{"events_received":1}'
        opener.return_value.__enter__.return_value = response
        self.assertTrue(deliver_event_once())
        event.refresh_from_db()
        self.assertEqual(event.status, 'sent')
        self.assertFalse(deliver_event_once())
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(lead.conversion_events.get(event_name='Contact').status, 'simulated')

    @patch('crm.automation.urlopen')
    def test_uncertain_delivery_is_not_retried(self, opener):
        lead = self.lead()
        self.meta_ready(lead)
        change_stage(lead, self.account.stages.get(name='Qualificado'), self.manager)
        opener.side_effect = TimeoutError()
        self.assertTrue(deliver_event_once())
        self.assertEqual(lead.conversion_events.get(event_name='Lead').status, 'review')
        self.assertFalse(deliver_event_once())
        self.assertEqual(opener.call_count, 1)

    @patch('crm.automation.urlopen')
    def test_configuration_change_blocks_pending_event_before_external_call(self, opener):
        lead = self.lead()
        self.meta_ready(lead)
        change_stage(lead, self.account.stages.get(name='Qualificado'), self.manager)
        AutomationConfig.objects.filter(account=self.account).update(mode='live')
        self.assertTrue(deliver_event_once())
        opener.assert_not_called()
        self.assertEqual(lead.conversion_events.get(event_name='Lead').status, 'blocked')

    def test_missing_ad_and_unsupported_event_are_blocked(self):
        lead = self.lead()
        self.meta_ready(lead, events=['Purchase'])
        change_stage(lead, self.account.stages.get(name='Qualificado'), self.manager)
        self.assertIn('não liberado', lead.conversion_events.get(event_name='Lead').reason)
        self.account.automation.allowed_events = ['Lead']
        self.account.automation.save()
        lead.attributions.all().delete()
        event = lead.conversion_events.get(event_name='Lead')
        prepare_event(event)
        self.assertIn('click ID', event.reason)

    @patch('crm.integrations.graph')
    def test_ad_names_are_validated_against_selected_account(self, graph):
        lead = self.lead()
        self.meta_ready(lead)
        attr = lead.attributions.get()
        attr.enrichment_status = 'pending'
        attr.save()
        graph.return_value = {'account_id': '99', 'name': 'Anúncio A', 'campaign': {'name': 'Campanha A'}, 'adset': {'name': 'Conjunto A'}}
        self.assertTrue(enrich_ad_once())
        attr.refresh_from_db()
        self.assertEqual(attr.ad_name, 'Anúncio A')
        self.assertEqual(attr.enrichment_status, 'done')
        attr.enrichment_status = 'pending'
        attr.save()
        graph.return_value['account_id'] = '666'
        enrich_ad_once()
        attr.refresh_from_db()
        self.assertEqual(attr.enrichment_status, 'mismatch')

    def test_pipeline_and_automation_permissions_and_csrf(self):
        self.lead()
        self.login()
        for route in ['funnel', 'automation_settings']:
            self.assertEqual(self.client.get(reverse(route, args=[self.account.id])).status_code, 200)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        self.assertEqual(client.post(reverse('automation_settings', args=[self.account.id]), {'action': 'config'}).status_code, 403)
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse('funnel', args=[self.account.id])).status_code, 200)
        self.assertEqual(self.client.post(reverse('funnel', args=[self.account.id]), {}).status_code, 403)
        self.assertEqual(self.client.get(reverse('automation_settings', args=[self.account.id])).status_code, 403)

    def test_ads_token_does_not_implicitly_authorize_conversions(self):
        from .services import meta_config
        MetaIntegration.objects.create(account=self.account, credentials=seal({'access_token': 'read-only-ads-token'}))
        self.assertEqual(meta_config(self.account)['access_token'], '')

    @patch('crm.automation.urlopen')
    def test_explicit_rejection_can_be_retried_with_same_id_but_uncertain_result_cannot(self, opener):
        from .automation import retry_blocked
        from django.core.exceptions import ValidationError
        lead = self.lead()
        self.meta_ready(lead)
        change_stage(lead, self.account.stages.get(name='Qualificado'), self.manager)
        event = lead.conversion_events.get(event_name='Lead')
        stable_id = event.id
        opener.side_effect = HTTPError('https://graph.facebook.com', 400, 'bad request', {}, None)
        deliver_event_once()
        event.refresh_from_db()
        self.assertEqual(event.status, 'failed')
        retry_blocked(event, self.manager)
        event.refresh_from_db()
        self.assertEqual(event.id, stable_id)
        self.assertEqual(event.status, 'pending')
        self.assertEqual(event.payload['data'][0]['event_id'], str(stable_id))
        event.status = 'review'
        event.save()
        with self.assertRaises(ValidationError):
            retry_blocked(event, self.manager)
