from django import forms
import uuid
from .models import Account, Connection, TrackableLink, Sale, Stage, MessageRule, AutomationConfig, Lead
from .automation import EVENTS


class AccountForm(forms.ModelForm):
    integration_key = forms.CharField(required=False, widget=forms.HiddenInput)

    def clean_integration_key(self):
        return self.cleaned_data.get('integration_key') or 'CLIENTE_' + uuid.uuid4().hex[:12].upper()
    class Meta:
        model = Account
        fields = ['name', 'integration_key']
        labels = {'integration_key': 'Identificador (ex.: CLIENTE_01)'}


class ConnectionForm(forms.ModelForm):
    class Meta:
        model = Connection
        fields = ['name', 'provider', 'instance_id', 'phone']
        labels = {'instance_id': 'ID da instância ou canal'}

    def clean(self):
        data = super().clean()
        if not data.get('phone'):
            self.add_error('phone', 'Informe o número conectado com DDI e DDD.')
        return data


class OmniForm(forms.Form):
    connection_id = forms.UUIDField(widget=forms.HiddenInput)
    secret_key = forms.CharField(label='Secret Key do workspace Omni', max_length=2000, required=False,
                                 widget=forms.PasswordInput, help_text='Deixe vazio para manter a chave cadastrada.')
    phone = forms.CharField(label='WhatsApp com DDI', max_length=15, required=False)

    def clean_phone(self):
        from .models import phone_validator
        value = self.cleaned_data['phone']
        if value:
            phone_validator(value)
        return value


class WhatsAppConnectForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    name = forms.CharField(label='Nome da conexão', max_length=100, initial='WhatsApp principal')
    mode = forms.ChoiceField(choices=[('qr', 'QR Code'), ('coexistence', 'Coexistência')], widget=forms.RadioSelect, initial='qr')


class OmniWorkspaceForm(forms.Form):
    secret_key = forms.CharField(label='Secret Key do workspace Omni', max_length=2000, required=False, widget=forms.PasswordInput)
    public_key = forms.CharField(label='Public Key do SDK (necessária para Coexistência)', max_length=2000, required=False)


class MetaForm(forms.Form):
    app_id = forms.CharField(label='App ID do Facebook', max_length=80, required=False)
    app_secret = forms.CharField(label='App Secret', max_length=2000, required=False, widget=forms.PasswordInput)
    access_token = forms.CharField(label='Token de anúncios (alternativa à autorização pelo Facebook)', max_length=8000,
                                   required=False, widget=forms.PasswordInput)
    conversion_token = forms.CharField(label='Token para envio de conversões (CAPI, acesso ao dataset)', max_length=8000,
                                      required=False, widget=forms.PasswordInput)
    dataset_id = forms.CharField(label='Dataset ID para conversões (opcional)', max_length=80, required=False)
    waba_id = forms.CharField(label='WABA ID para conversões elegíveis (opcional)', max_length=80, required=False)
    test_event_code = forms.CharField(label='Código de teste de conversões (opcional)', max_length=100, required=False)

    def clean(self):
        data = super().clean()
        for key in ('app_id', 'dataset_id', 'waba_id'):
            if data.get(key) and not data[key].isdigit():
                self.add_error(key, 'Use somente dígitos.')
        return data


class LinkForm(forms.ModelForm):
    class Meta:
        model = TrackableLink
        fields = ['connection', 'name', 'text', 'source', 'medium', 'campaign']
        labels = {'connection': 'WhatsApp de destino', 'name': 'Nome do link',
                  'source': 'Onde você vai divulgar? (ex.: instagram, site, indicação)',
                  'text': 'Mensagem que a pessoa verá no WhatsApp', 'campaign': 'Nome da campanha (opcional)',
                  'medium': 'Meio (opcional, ex.: bio, cpc)'}

    def __init__(self, *args, account, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['connection'].queryset = account.connections.filter(active=True).exclude(phone='')


class SaleForm(forms.ModelForm):
    request_id = forms.UUIDField(widget=forms.HiddenInput)

    class Meta:
        model = Sale
        fields = ['amount']
        labels = {'amount': 'Valor da venda (R$)'}
        widgets = {'amount': forms.NumberInput(attrs={'min': '0.01', 'step': '0.01'})}


class DealValueForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = ['deal_value']
        labels = {'deal_value': 'Valor negociado (R$)'}
        widgets = {'deal_value': forms.NumberInput(attrs={'min': '0.01', 'step': '0.01'})}


class StageForm(forms.ModelForm):
    event_name = forms.ChoiceField(label='Evento ao entrar nesta etapa', required=False,
        choices=[('', 'Nenhum evento')] + [(e, e) for e in EVENTS])
    class Meta:
        model = Stage
        fields = ['name', 'position', 'won', 'event_name', 'archived']
        labels = {'position': 'Ordem', 'won': 'Representa uma venda'}


class AutomationConfigForm(forms.ModelForm):
    allowed_events = forms.MultipleChoiceField(label='Eventos liberados para envio neste canal', required=False,
        choices=[(e, e) for e in EVENTS], widget=forms.CheckboxSelectMultiple)
    confirm = forms.BooleanField(label='Confirmei ativos, permissão de envio e suporte aos eventos na Meta.', required=False)

    class Meta:
        model = AutomationConfig
        fields = ['mode', 'allowed_events']
        labels = {'mode': 'Modo de operação'}

    def clean(self):
        data = super().clean()
        if data.get('mode') != 'simulation' and not data.get('confirm'):
            self.add_error('confirm', 'Confirme a configuração antes de ativar envios externos.')
        return data


class MessageRuleForm(forms.ModelForm):
    class Meta:
        model = MessageRule
        fields = ['name', 'phrase', 'match_type', 'direction', 'from_stage', 'to_stage', 'priority', 'fixed_value', 'active']
        labels = {'name': 'Nome da regra', 'phrase': 'Frase gatilho', 'match_type': 'Como comparar',
                  'direction': 'Quem envia a mensagem', 'from_stage': 'Somente nesta etapa (opcional)',
                  'to_stage': 'Mover para', 'priority': 'Prioridade (menor primeiro)', 'active': 'Ativa',
                  'fixed_value': 'Valor fixo da venda (R$, opcional; usado se não houver valor negociado)'}

    def __init__(self, *args, account, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.account = account
        for name in ('from_stage', 'to_stage'):
            self.fields[name].queryset = account.stages.filter(archived=False)

    def clean_phrase(self):
        from .automation import normalize
        value = self.cleaned_data['phrase'].strip()
        if len(normalize(value)) < 3:
            raise forms.ValidationError('Use uma frase específica com pelo menos três caracteres.')
        return value
