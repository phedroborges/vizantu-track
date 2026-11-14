from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Sum
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from .models import Lead, Sale
from .views import access, available_accounts, page


# These describe navigation and empty states, not integrations already implemented.
CHANNELS = {
    'paid': {'title': 'Tráfego pago', 'label': 'Aquisição', 'description': 'Investimento, campanhas e resultados dos anúncios.',
             'metrics': ['Investimento', 'Impressões', 'Cliques', 'Conversões da plataforma'],
             'sources': ['Meta Ads', 'Google Ads'], 'next': 'Conectar Google Ads e consolidar os resultados com os anúncios da Meta.'},
    'organic': {'title': 'Redes sociais', 'label': 'Orgânico', 'description': 'Conteúdo, alcance e relacionamento em cada rede.',
                'metrics': ['Alcance', 'Interações', 'Seguidores', 'Publicações'],
                'sources': ['Instagram', 'Facebook', 'TikTok', 'YouTube', 'LinkedIn'], 'next': 'Autorizar os perfis de cada cliente para importar publicações e métricas disponíveis.'},
    'local': {'title': 'Presença local', 'label': 'Google Meu Negócio', 'description': 'Como as pessoas encontram e interagem com a empresa no Google.',
              'metrics': ['Visualizações do perfil', 'Cliques no site', 'Cliques para ligar', 'Solicitações de rota'],
              'sources': ['Google Meu Negócio'], 'next': 'Conectar os perfis da empresa. Interações no Google serão separadas de visitas e vendas confirmadas.'},
    'site': {'title': 'Site e e-commerce', 'label': 'Experiência digital', 'description': 'Da visita à conversão no site ou na loja virtual.',
             'metrics': ['Sessões', 'Contatos no site', 'Pedidos', 'Receita online'],
             'sources': ['Google Analytics 4', 'Eventos do site', 'Plataforma de e-commerce'], 'next': 'Conectar o Analytics e configurar eventos e identificadores para associar as conversões ao CRM.'},
    'attribution': {'title': 'Jornada e atribuição', 'label': 'Da origem à receita', 'description': 'Post ou anúncio → visita ou mensagem → contato → oportunidade → venda.',
                    'metrics': ['Receita atribuída', 'Vendas identificadas', 'Custo por venda', 'ROAS atribuído'],
                    'sources': ['Links e UTMs', 'Identificadores de anúncios', 'WhatsApp', 'Site', 'ERP'], 'next': 'Consolidar as evidências de origem e as vendas. O que não puder ser associado ficará como origem não identificada.'},
}


def portal_accounts(user):
    return available_accounts(user).exclude(integration_key__in=['DEMO_AURORA', 'DEMO_HORIZONTE'])


@login_required
@require_GET
def agency(request):
    accounts = portal_accounts(request.user).select_related('meta').annotate(
        contact_count=Count('connections__leads', distinct=True)).order_by('name')
    since = timezone.now() - timedelta(days=30)
    leads = Lead.objects.filter(connection__account__in=accounts, first_message_at__gte=since)
    sales = Sale.objects.filter(lead__connection__account__in=accounts, occurred_at__gte=since)
    # Keep currencies separate; summing different currencies would misstate revenue.
    revenue = sales.order_by().values('currency').annotate(total=Sum('amount')).order_by('currency')
    return render(request, 'crm/agency.html', {
        'accounts': accounts, 'client_count': accounts.count(), 'contact_count': leads.count(),
        'sale_count': sales.count(), 'revenue_by_currency': revenue,
        'recent_sales': sales.select_related('lead__connection__account').order_by('-occurred_at')[:5],
    })


@login_required
@require_GET
def dashboards(request, account_id=None):
    context = {'channels': CHANNELS.items()}
    if account_id:
        account, role = access(request, account_id)
        return page(request, 'crm/dashboards.html', account, role, **context)
    return render(request, 'crm/dashboards.html', {'accounts': portal_accounts(request.user).order_by('name'), **context})


@login_required
@require_GET
def channel(request, account_id, section):
    account, role = access(request, account_id)
    if section not in CHANNELS:
        raise Http404
    return page(request, 'crm/channel_report.html', account, role, section=section, report=CHANNELS[section])


@login_required
@require_GET
def sales(request, account_id):
    account, role = access(request, account_id)
    since = timezone.now() - timedelta(days=30)
    records = Sale.objects.filter(lead__connection__account=account, occurred_at__gte=since).select_related('lead').order_by('-occurred_at')
    return page(request, 'crm/sales.html', account, role,
                items=Paginator(records, 25).get_page(request.GET.get('page')),
                revenue_by_currency=records.order_by().values('currency').annotate(total=Sum('amount')).order_by('currency'))


@login_required
@require_GET
def integrations(request, account_id):
    account, role = access(request, account_id)
    return page(request, 'crm/integrations.html', account, role,
                meta=account.meta if hasattr(account, 'meta') else None,
                whatsapp_ready=account.connections.filter(active=True, last_received_at__isnull=False).exists())
