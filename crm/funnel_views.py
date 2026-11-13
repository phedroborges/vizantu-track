from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from .automation import retry_blocked, normalize
from .forms import StageForm, MessageRuleForm, AutomationConfigForm
from .models import AutomationConfig, Audit, ConversionEvent, Lead, MessageRule
from .services import change_stage
from .views import access, page, lead_query


@login_required
@require_http_methods(['GET', 'POST'])
def board(request, account_id):
    account, role = access(request, account_id)
    if request.method == 'POST':
        if role == 'viewer':
            raise PermissionDenied
        lead = get_object_or_404(Lead, pk=request.POST.get('lead_id'), connection__account=account)
        stage = get_object_or_404(account.stages, pk=request.POST.get('stage_id'), archived=False)
        change_stage(lead, stage, request.user)
        return redirect('funnel', account_id=account.id)
    columns = []
    for stage in account.stages.all():
        if stage.archived and not stage.leads.exists():
            continue
        contacts = lead_query(account).filter(stage=stage).order_by('-last_message_at')
        columns.append({'stage': stage, 'count': contacts.count(), 'leads': contacts[:50]})
    return page(request, 'crm/funnel.html', account, role, columns=columns,
                stages=account.stages.filter(archived=False))


@login_required
@require_http_methods(['GET', 'POST'])
def automation_settings(request, account_id):
    account, role = access(request, account_id, ['manager'])
    config, _ = AutomationConfig.objects.get_or_create(account=account)
    config_form = AutomationConfigForm(instance=config)
    rule_form = MessageRuleForm(account=account)
    stage_form = StageForm(auto_id='new-stage-%s')
    preview = None
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'config':
            config_form = AutomationConfigForm(request.POST, instance=config)
            if config_form.is_valid():
                obj = config_form.save(commit=False)
                obj.activated_at = timezone.now()
                obj.save()
                Audit.objects.create(account=account, actor=request.user, action='automation.configured', object_id=str(obj.pk), detail={'mode': obj.mode})
                messages.success(request, 'Modo de operação salvo. O histórico não será reenviado automaticamente.')
                return redirect('automation_settings', account_id=account.id)
        elif action == 'rule':
            rule = get_object_or_404(account.message_rules, pk=request.POST['rule_id']) if request.POST.get('rule_id') else None
            rule_form = MessageRuleForm(request.POST, instance=rule, account=account,
                auto_id=f'rule-{rule.pk}-%s' if rule else 'id_%s')
            if rule_form.is_valid():
                obj = rule_form.save(commit=False)
                obj.activated_at = timezone.now()
                obj.save()
                Audit.objects.create(account=account, actor=request.user, action='automation.rule_saved', object_id=str(obj.pk))
                return redirect('automation_settings', account_id=account.id)
        elif action == 'toggle':
            rule = get_object_or_404(account.message_rules, pk=request.POST.get('rule_id'))
            rule.active = not rule.active
            rule.activated_at = timezone.now()
            rule.save(update_fields=['active', 'activated_at'])
            return redirect('automation_settings', account_id=account.id)
        elif action == 'stage':
            stage = get_object_or_404(account.stages, pk=request.POST['stage_id']) if request.POST.get('stage_id') else None
            stage_form = StageForm(request.POST, instance=stage, auto_id=f'stage-{stage.pk}-%s' if stage else 'new-stage-%s')
            if stage_form.is_valid():
                obj = stage_form.save(commit=False)
                obj.account = account
                try:
                    obj.full_clean()
                    if obj.archived and account.stages.filter(archived=False).exclude(pk=obj.pk).count() == 0:
                        raise ValidationError('Mantenha pelo menos uma etapa ativa.')
                    obj.save()
                    Audit.objects.create(account=account, actor=request.user, action='funnel.stage_saved', object_id=str(obj.pk))
                    return redirect('automation_settings', account_id=account.id)
                except ValidationError as exc:
                    stage_form.add_error(None, exc)
        elif action == 'preview':
            text = normalize(request.POST.get('text', '')[:64000])
            preview = [r for r in account.message_rules.filter(active=True, direction=request.POST.get('direction', 'out')).select_related('to_stage')
                if (normalize(r.phrase) == text if r.match_type == 'exact' else normalize(r.phrase) in text)]
        elif action == 'retry':
            event = get_object_or_404(ConversionEvent, pk=request.POST.get('event_id'), lead__connection__account=account)
            try:
                retry_blocked(event, request.user)
                messages.success(request, 'Evento reavaliado. Confira o estado na fila.')
            except ValidationError as exc:
                messages.error(request, '; '.join(exc.messages))
            return redirect('automation_settings', account_id=account.id)
    rules = list(account.message_rules.select_related('from_stage', 'to_stage').order_by('priority', 'id'))
    for rule in rules:
        rule.edit_form = rule_form if rule_form.instance.pk == rule.pk else MessageRuleForm(instance=rule, account=account, auto_id=f'rule-{rule.pk}-%s')
    return page(request, 'crm/automation.html', account, role, config=config, config_form=config_form,
        rule_form=MessageRuleForm(account=account) if rule_form.instance.pk else rule_form,
        stage_form=StageForm(auto_id='new-stage-%s') if stage_form.instance.pk else stage_form,
        stage_forms=[(s, stage_form if stage_form.instance.pk == s.pk else StageForm(instance=s, auto_id=f'stage-{s.pk}-%s')) for s in account.stages.all()],
        rules=rules,
        events=Paginator(ConversionEvent.objects.filter(lead__connection__account=account).select_related('lead', 'transition__message', 'transition__rule').order_by('-occurred_at'), 30).get_page(request.GET.get('page')),
        preview=preview)
