from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Connection, Lead, Sale
from .services import create_account


class PortalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user('portal-manager')
        cls.other_user = get_user_model().objects.create_user('other-manager')
        cls.account = create_account('Minha empresa', 'PORTAL_A', cls.user)
        cls.other = create_account('Cliente privado', 'PORTAL_B', cls.other_user)
        cls.demo = create_account('Demonstração', 'DEMO_AURORA', cls.user)
        for account, amount in [(cls.account, '125.50'), (cls.other, '9999'), (cls.demo, '5000')]:
            conn = Connection.objects.create(account=account, name='Comercial', instance_id=str(account.id))
            lead = Lead.objects.create(connection=conn, stage=account.stages.first(), phone='5511999999999',
                                       first_message_at=timezone.now(), last_message_at=timezone.now())
            Sale.objects.create(lead=lead, amount=Decimal(amount), request_id=uuid.uuid4())

    def setUp(self):
        self.client.force_login(self.user)

    def test_agency_only_aggregates_accessible_real_clients(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.context['client_count'], 1)
        self.assertEqual(response.context['contact_count'], 1)
        self.assertEqual(response.context['sale_count'], 1)
        self.assertEqual(list(response.context['revenue_by_currency']), [{'currency': 'BRL', 'total': Decimal('125.50')}])
        self.assertNotContains(response, 'Cliente privado')
        self.assertNotContains(response, '>Demonstração<')

    def test_all_portal_pages_render_and_require_membership(self):
        for name in ['client_dashboards', 'sales', 'integrations']:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name, args=[self.account.id])).status_code, 200)
                self.assertEqual(self.client.get(reverse(name, args=[self.other.id])).status_code, 404)
        for section in ['paid', 'organic', 'local', 'site', 'attribution']:
            with self.subTest(section=section):
                response = self.client.get(reverse('channel_report', args=[self.account.id, section]))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Aguardando integração')
                self.assertEqual(self.client.get(reverse('channel_report', args=[self.other.id, section])).status_code, 404)
        self.assertEqual(self.client.get(reverse('channel_report', args=[self.account.id, 'invalid'])).status_code, 404)
        for name in ['clients', 'agency_dashboards']:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_read_only_pages_do_not_accept_post_and_require_login(self):
        url = reverse('sales', args=[self.account.id])
        self.assertEqual(self.client.post(url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.client.get(reverse('home')).status_code, 302)

    def test_sales_are_isolated_and_currencies_are_not_combined(self):
        lead = self.account.connections.first().leads.first()
        Sale.objects.create(lead=lead, amount=Decimal('10'), currency='USD', request_id=uuid.uuid4())
        response = self.client.get(reverse('sales', args=[self.account.id]))
        self.assertEqual(response.context['items'].paginator.count, 2)
        self.assertEqual(list(response.context['revenue_by_currency']), [
            {'currency': 'BRL', 'total': Decimal('125.50')}, {'currency': 'USD', 'total': Decimal('10')}])
