from django import forms
from .models import Account, Connection, TrackableLink, Sale, Stage


class AccountForm(forms.ModelForm):
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


class MetaForm(forms.Form):
    app_id = forms.CharField(label='App ID do Facebook', max_length=80, required=False)
    app_secret = forms.CharField(label='App Secret', max_length=2000, required=False, widget=forms.PasswordInput)
    access_token = forms.CharField(label='Token de anúncios (alternativa à autorização pelo Facebook)', max_length=8000,
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
        labels = {'connection': 'WhatsApp de destino'}

    def __init__(self, *args, account, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['connection'].queryset = account.connections.filter(active=True)


class SaleForm(forms.ModelForm):
    request_id = forms.UUIDField(widget=forms.HiddenInput)

    class Meta:
        model = Sale
        fields = ['amount']
        labels = {'amount': 'Valor da venda (R$)'}
        widgets = {'amount': forms.NumberInput(attrs={'min': '0.01', 'step': '0.01'})}


class StageForm(forms.ModelForm):
    class Meta:
        model = Stage
        fields = ['name', 'position', 'won']
        labels = {'position': 'Ordem', 'won': 'Representa uma venda'}
