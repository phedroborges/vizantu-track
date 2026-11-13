import uuid
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

phone_validator = RegexValidator(r'^[1-9][0-9]{7,14}$', 'Use DDI e número, somente dígitos (8 a 15).')


class Account(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField('Nome', max_length=120)
    integration_key = models.CharField(max_length=40, unique=True, validators=[
        RegexValidator(r'^[A-Z][A-Z0-9_]{1,39}$', 'Use letras maiúsculas, números e _.')])
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.name


class Membership(models.Model):
    ROLES = [('manager', 'Gestor'), ('operator', 'Operador'), ('viewer', 'Leitura')]
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=12, choices=ROLES, default='operator')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account', 'user'], name='member_account_user')]


class Connection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='connections')
    name = models.CharField('Nome', max_length=100)
    instance_id = models.CharField('ID da instância Z-API', max_length=120, unique=True)
    phone = models.CharField('WhatsApp com DDI', max_length=15, validators=[phone_validator], blank=True)
    token_hash = models.CharField(max_length=64, editable=False)
    active = models.BooleanField(default=True)
    last_received_at = models.DateTimeField(null=True, blank=True)
    provider = models.CharField(max_length=12, default='zapi', choices=[('zapi', 'Z-API tradicional'), ('omni', 'Z-API Omni')])
    credentials = models.TextField(blank=True, editable=False)
    webhook_id = models.CharField(max_length=120, blank=True)

    def __str__(self):
        return f'{self.account} / {self.name}'


class Stage(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='stages')
    name = models.CharField('Etapa', max_length=60)
    position = models.PositiveSmallIntegerField(default=0)
    won = models.BooleanField(default=False)

    class Meta:
        ordering = ['position', 'id']
        constraints = [models.UniqueConstraint(fields=['account', 'name'], name='stage_account_name')]

    def __str__(self):
        return self.name


class Lead(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT, related_name='leads')
    phone = models.CharField(max_length=15, validators=[phone_validator])
    name = models.CharField(max_length=150, blank=True)
    stage = models.ForeignKey(Stage, on_delete=models.PROTECT, related_name='leads')
    first_message_at = models.DateTimeField()
    last_message_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'phone'], name='lead_connection_phone')]
        ordering = ['-last_message_at']

    def clean(self):
        if self.stage_id and self.connection_id and self.stage.account_id != self.connection.account_id:
            raise ValidationError('Etapa e conexão precisam pertencer ao mesmo cliente.')

    def __str__(self):
        return self.name or self.phone


class Message(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='messages')
    external_id = models.CharField(max_length=200)
    direction = models.CharField(max_length=3, choices=[('in', 'Recebida'), ('out', 'Enviada')])
    kind = models.CharField(max_length=30, default='text')
    text = models.TextField(blank=True)
    occurred_at = models.DateTimeField(db_index=True)
    edited = models.BooleanField(default=False)
    deleted = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['lead', 'external_id'], name='message_lead_external')]
        ordering = ['occurred_at', 'id']


class TrackableLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT, related_name='links')
    name = models.CharField('Nome da campanha', max_length=100)
    text = models.CharField('Mensagem inicial', max_length=600, default='Olá! Quero saber mais.')
    source = models.CharField('Origem', max_length=200, default='site')
    medium = models.CharField('Meio', max_length=200, blank=True)
    campaign = models.CharField('Campanha', max_length=200, blank=True)
    active = models.BooleanField(default=True)


class Click(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    link = models.ForeignKey(TrackableLink, on_delete=models.CASCADE, related_name='clicks')
    source = models.CharField(max_length=200, blank=True)
    medium = models.CharField(max_length=200, blank=True)
    campaign = models.CharField(max_length=200, blank=True)
    content = models.CharField(max_length=200, blank=True)
    gclid = models.CharField(max_length=500, blank=True)
    fbclid = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    matched_lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.SET_NULL)


class Attribution(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='attributions')
    click = models.OneToOneField(Click, null=True, blank=True, on_delete=models.SET_NULL, related_name='attribution')
    message = models.OneToOneField(Message, on_delete=models.CASCADE)
    source = models.CharField(max_length=200)
    medium = models.CharField(max_length=200, blank=True)
    campaign = models.CharField(max_length=200, blank=True)
    content = models.CharField(max_length=200, blank=True)
    gclid = models.CharField(max_length=500, blank=True)
    fbclid = models.CharField(max_length=500, blank=True)
    ad_id = models.CharField(max_length=200, blank=True)
    ctwa_clid = models.CharField(max_length=1000, blank=True)
    evidence = models.CharField(max_length=20, choices=[('message_code', 'Código recebido na mensagem'),
                                                    ('ad_referral', 'Anúncio informado pela Z-API')])
    occurred_at = models.DateTimeField()

    class Meta:
        ordering = ['occurred_at', 'id']


class Sale(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lead = models.ForeignKey(Lead, on_delete=models.PROTECT, related_name='sales')
    request_id = models.UUIDField(unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    currency = models.CharField(max_length=3, default='BRL')
    occurred_at = models.DateTimeField(default=timezone.now)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name='sale_positive')]


class Dispatch(models.Model):
    STATUS = [('pending', 'Na fila'), ('sending', 'Enviando'), ('sent', 'Aceito pela Meta'),
              ('failed', 'Falhou'), ('review', 'Resultado incerto: conferir na Meta')]
    sale = models.OneToOneField(Sale, on_delete=models.PROTECT, related_name='dispatch')
    status = models.CharField(max_length=12, choices=STATUS, default='pending')
    payload = models.JSONField()
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    result = models.CharField(max_length=500, blank=True)


class Audit(models.Model):
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='audit')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=60)
    object_id = models.CharField(max_length=80)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)


class WebhookEvent(models.Model):
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT, related_name='events')
    digest = models.CharField(max_length=64)
    payload = models.JSONField()
    status = models.CharField(max_length=10, default='pending', db_index=True)
    result = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'digest'], name='event_connection_digest')]


class LoginAttempt(models.Model):
    key = models.CharField(primary_key=True, max_length=64)
    failures = models.PositiveIntegerField(default=0)
    window_start = models.DateTimeField(default=timezone.now)


class MetaIntegration(models.Model):
    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name='meta')
    credentials = models.TextField(blank=True, editable=False)
    ad_account_id = models.CharField(max_length=80, blank=True)
    ad_account_name = models.CharField(max_length=200, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    sync_status = models.CharField(max_length=12, default='idle')
    sync_result = models.CharField(max_length=300, blank=True)
    sync_started_at = models.DateTimeField(null=True)
    last_synced_at = models.DateTimeField(null=True)


class AdInsight(models.Model):
    integration = models.ForeignKey(MetaIntegration, on_delete=models.CASCADE, related_name='insights')
    ad_account_id = models.CharField(max_length=80)
    date = models.DateField()
    ad_id = models.CharField(max_length=80)
    ad_name = models.CharField(max_length=500)
    adset_id = models.CharField(max_length=80)
    adset_name = models.CharField(max_length=500)
    campaign_id = models.CharField(max_length=80)
    campaign_name = models.CharField(max_length=500)
    spend = models.DecimalField(max_digits=16, decimal_places=4)
    impressions = models.PositiveBigIntegerField(default=0)
    clicks = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['integration', 'ad_account_id', 'date', 'ad_id'], name='insight_daily_ad')]
