from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Sum
from django.http import Http404
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.db.models.functions import TruncDate
from django.db.models import F
from django.views.decorators.http import require_GET

from .models import Lead, Sale, AdInsight
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
    available = portal_accounts(request.user).select_related('meta').order_by('name')
    today = timezone.localdate()
    period = request.GET.get('period', '30')
    try:
        if period == 'custom':
            start = parse_date(request.GET.get('start', ''))
            end = parse_date(request.GET.get('end', ''))
        else:
            days = int(period)
            if days not in (7, 30, 90):
                raise ValueError
            start, end = today - timedelta(days=days - 1), today
        if not start or not end or start > end or (end - start).days > 366:
            raise ValueError
    except (ValueError, TypeError):
        from django.http import HttpResponse
        return HttpResponse('Período inválido. Selecione início e fim, com intervalo de até 366 dias.', status=400)
    selected = request.GET.getlist('project')
    allowed = {str(item.id) for item in available}
    if any(item not in allowed for item in selected):
        raise Http404
    accounts = available.filter(id__in=selected) if selected else available
    accounts = list(accounts)
    scope = [item.id for item in accounts]
    leads = Lead.objects.filter(connection__account_id__in=scope, first_message_at__date__range=(start, end))
    sales = Sale.objects.filter(lead__connection__account_id__in=scope, occurred_at__date__range=(start, end))
    insights = AdInsight.objects.filter(integration__account_id__in=scope,
        ad_account_id=F('integration__ad_account_id'), date__range=(start, end))
    currencies = sorted(set(insights.values_list('integration__currency', flat=True)))
    currency = request.GET.get('currency', 'BRL')
    if currency not in currencies and currencies:
        currency = currencies[0]
    insights = insights.filter(integration__currency=currency)
    paid = insights.aggregate(spend=Sum('spend'), impressions=Sum('impressions'), clicks=Sum('clicks'))
    revenue = list(sales.order_by().values('currency').annotate(total=Sum('amount')).order_by('currency'))
    lead_days = dict(leads.order_by().annotate(day=TruncDate('first_message_at')).values_list('day').annotate(total=Count('id')))
    sale_days = dict(sales.order_by().annotate(day=TruncDate('occurred_at')).values_list('day').annotate(total=Count('id')))
    # Fill missing dates for CRM counts; missing ad coverage is never represented as zero spend.
    daily = [{'date': start + timedelta(days=i), 'contacts': lead_days.get(start + timedelta(days=i), 0),
              'sales': sale_days.get(start + timedelta(days=i), 0)} for i in range((end-start).days+1)]
    peak = max([row['contacts'] for row in daily] + [row['sales'] for row in daily] + [1])
    for row in daily:
        row['contact_height'] = round(row['contacts'] / peak * 100)
        row['sale_height'] = round(row['sales'] / peak * 100)
    by_lead = {row['connection__account_id']: row['total'] for row in leads.order_by().values('connection__account_id').annotate(total=Count('id'))}
    by_sales = {row['lead__connection__account_id']: row['total'] for row in sales.order_by().values('lead__connection__account_id').annotate(total=Count('id'))}
    by_spend = {row['integration__account_id']: row['total'] for row in insights.order_by().values('integration__account_id').annotate(total=Sum('spend'))}
    for item in accounts:
        item.contact_count = by_lead.get(item.id, 0)
        item.sale_count = by_sales.get(item.id, 0)
        item.spend = by_spend.get(item.id)
    max_contacts = max([item.contact_count for item in accounts] + [1])
    for item in accounts:
        item.contact_width = round(item.contact_count / max_contacts * 100)
    focus = request.GET.get('focus', 'all')
    if focus not in ('all', 'paid', 'organic', 'physical'):
        raise Http404
    return render(request, 'crm/agency.html', {
        'accounts': accounts, 'available_projects': [{'id': str(item.id), 'name': item.name, 'selected': str(item.id) in selected} for item in available],
        'client_count': len(accounts), 'contact_count': leads.count(), 'sale_count': sales.count(),
        'revenue_by_currency': revenue, 'daily': daily, 'paid': paid, 'currency': currency,
        'currencies': currencies, 'period': period, 'start': start.isoformat(), 'end': end.isoformat(), 'focus': focus,
        'selected_count': len(selected),
    })


@login_required
@require_GET
def dashboards(request, account_id=None):
    if account_id:
        account, role = access(request, account_id)
        target = '/a/' + str(account.id) + '/'
        if request.GET:
            target += '?' + request.GET.urlencode()
        return redirect(target)
    target = '/'
    if request.GET:
        target += '?' + request.GET.urlencode()
    return redirect(target)


@login_required
@require_GET
def channel(request, account_id, section):
    account, role = access(request, account_id)
    if section == 'paid':
        from .meta_views import ads_report
        return ads_report(request, account_id)
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
