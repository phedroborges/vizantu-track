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
        fields = ['name', 'instance_id', 'phone']


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
