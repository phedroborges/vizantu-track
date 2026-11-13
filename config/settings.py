import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv('DEBUG', '0') == '1'
SECRET_KEY = os.environ.get('SECRET_KEY', '')
if len(SECRET_KEY) < 32:
    raise ImproperlyConfigured('Defina SECRET_KEY com pelo menos 32 caracteres aleatórios.')
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')
CSRF_TRUSTED_ORIGINS = [x for x in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if x]
INSTALLED_APPS = ['django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
                  'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'crm']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'whitenoise.middleware.WhiteNoiseMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware',
              'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
              'django.contrib.auth.middleware.AuthenticationMiddleware', 'django.contrib.messages.middleware.MessageMiddleware',
              'django.middleware.clickjacking.XFrameOptionsMiddleware', 'crm.middleware.LoginThrottleMiddleware']
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'templates'],
              'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request',
              'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages']}}]
WSGI_APPLICATION = 'config.wsgi.application'
db_url = os.getenv('DATABASE_URL')
DATABASE_SCHEMA = os.getenv('DATABASE_SCHEMA', '')
if DATABASE_SCHEMA and (not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', DATABASE_SCHEMA)
        or DATABASE_SCHEMA in {'public', 'auth', 'storage', 'extensions', 'realtime', 'vault', 'graphql', 'graphql_public', 'supabase_functions'}
        or DATABASE_SCHEMA.startswith(('pg_', 'supabase_'))):
    raise ImproperlyConfigured('DATABASE_SCHEMA deve ser um schema privado do aplicativo, como track.')
if db_url:
    db = urlparse(db_url)
    if db.scheme not in ('postgres', 'postgresql'):
        raise ImproperlyConfigured('DATABASE_URL deve apontar para PostgreSQL.')
    DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': unquote(db.path.lstrip('/')),
                 'USER': unquote(db.username or ''), 'PASSWORD': unquote(db.password or ''),
                 'HOST': db.hostname, 'PORT': db.port or 5432, 'CONN_MAX_AGE': 60,
                 'OPTIONS': {k: v[-1] for k, v in parse_qs(db.query).items() if k == 'sslmode'}}}
    if DATABASE_SCHEMA:
        DATABASES['default']['OPTIONS']['options'] = '-c search_path=' + DATABASE_SCHEMA
elif DEBUG:
    DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3',
                 'NAME': os.getenv('SQLITE_PATH', str(BASE_DIR / 'local.sqlite3')), 'OPTIONS': {'timeout': 20}}}
else:
    raise ImproperlyConfigured('PostgreSQL (DATABASE_URL) é obrigatório em produção.')
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGE_CODE = 'pt-br'
TIME_ZONE = 'America/Sao_Paulo'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 43200
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
# EasyPanel terminates TLS. Do not expose Gunicorn directly to the internet.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if not DEBUG else None
DATA_UPLOAD_MAX_MEMORY_SIZE = 512 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 100
META_GRAPH_VERSION = os.getenv('META_GRAPH_VERSION', 'v25.0')
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')
META_REDIRECT_URI = os.getenv('META_REDIRECT_URI', '')
LOGGING = {
    'version': 1, 'disable_existing_loggers': False,
    'filters': {'request_secrets': {'()': 'crm.logging.RedactRequestSecrets'}},
    'handlers': {'local_server': {'class': 'logging.StreamHandler', 'filters': ['request_secrets']}},
    'loggers': {'django.server': {'handlers': ['local_server'], 'level': 'INFO', 'propagate': False}},
}
