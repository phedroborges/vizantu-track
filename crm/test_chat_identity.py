from django.core.exceptions import ValidationError
from .tests import TenantFixture
from .models import Lead, MessageRule
from .services import ingest_message


class ChatIdentityTests(TenantFixture):
    def test_distinct_lids_do_not_become_same_phone_or_contact(self):
        for n in ['123456789', '987654321']:
            ingest_message(self.conn, self.payload(phone=n+'@lid', messageId=n))
        self.assertEqual(self.conn.leads.count(), 2)
        self.assertEqual(self.conn.leads.exclude(phone='').count(), 0)

    def test_explicit_lid_and_phone_pair_links_same_contact(self):
        ingest_message(self.conn, self.payload(phone='123456789@lid'))
        lead = self.conn.leads.get()
        ingest_message(self.conn, self.payload(phone='5511977777777', chatLid='123456789@lid', messageId='phone-known'))
        lead.refresh_from_db()
        self.assertEqual(lead.phone, '5511977777777')
        self.assertEqual(self.conn.leads.count(), 1)
        with self.assertRaises(ValueError):
            ingest_message(self.conn, self.payload(phone='5511988888888', chatLid='123456789@lid', messageId='conflict'))

    def test_outbound_lid_message_can_trigger_simulation_without_fake_phone(self):
        stage = self.account.stages.get(name='Qualificado')
        MessageRule.objects.create(account=self.account, name='Teste', phrase='RASTRO TESTE',
            direction='out', match_type='exact', to_stage=stage)
        payload = self.payload(phone='123456789@lid', fromMe=True, text={'message':'RASTRO TESTE'})
        ingest_message(self.conn, payload)
        ingest_message(self.conn, payload)
        lead = self.conn.leads.get()
        self.assertEqual(lead.stage, stage)
        self.assertEqual(lead.phone, '')
        self.assertEqual(lead.conversion_events.get().status, 'simulated')
        self.assertEqual(lead.messages.count(), 1)
