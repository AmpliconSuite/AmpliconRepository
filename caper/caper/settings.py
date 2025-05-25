import os
from django.utils.translation import gettext_lazy as _
import logging

logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s',
                    level=logging.DEBUG, datefmt='%Y-%m-%d %H:%M:%S')

######################
# MEZZANINE SETTINGS #
######################

# The following settings are already defined with default values in
# the ``defaults.py`` module within each of Mezzanine's apps, but are
# common enough to be put here, commented out, for conveniently
# overriding. Please consult the settings documentation for a full list
# of settings Mezzanine implements:
# http://mezzanine.jupo.org/docs/configuration.html#default-settings

# Controls the ordering and grouping of the admin menu.
#
# ADMIN_MENU_ORDER = (
#     ("Content", ("pages.Page", "blog.BlogPost",
#        "generic.ThreadedComment", (_("Media Library"), "media-library"),)),
#     ("Site", ("sites.Site", "redirects.Redirect", "conf.Setting")),
#     ("Users", ("auth.User", "auth.Group",)),
# )

# A three item sequence, each containing a sequence of template tags
# used to render the admin dashboard.
#
# DASHBOARD_TAGS = (
#     ("blog_tags.quick_blog", "mezzanine_tags.app_list"),
#     ("comment_tags.recent_comments",),
#     ("mezzanine_tags.recent_actions",),
# )

# A sequence of templates used by the ``page_menu`` template tag. Each
# item in the sequence is a three item sequence, containing a unique ID
# for the template, a label for the template, and the template path.
# These templates are then available for selection when editing which
# menus a page should appear in. Note that if a menu template is used
# that doesn't appear in this setting, all pages will appear in it.

# PAGE_MENU_TEMPLATES = (
#     (1, _("Top navigation bar"), "pages/menus/dropdown.html"),
#     (2, _("Left-hand tree"), "pages/menus/tree.html"),
#     (3, _("Footer"), "pages/menus/footer.html"),
# )

# A sequence of fields that will be injected into Mezzanine's (or any
# library's) models. Each item in the sequence is a four item sequence.
# The first two items are the dotted path to the model and its field
# name to be added, and the dotted path to the field class to use for
# the field. The third and fourth items are a sequence of positional
# args and a dictionary of keyword args, to use when creating the
# field instance. When specifying the field class, the path
# ``django.models.db.`` can be omitted for regular Django model fields.
#
# EXTRA_MODEL_FIELDS = (
#     (
#         # Dotted path to field.
#         "mezzanine.blog.models.BlogPost.image",
#         # Dotted path to field class.
#         "somelib.fields.ImageField",
#         # Positional args for field class.
#         (_("Image"),),
#         # Keyword args for field class.
#         {"blank": True, "upload_to": "blog"},
#     ),
#     # Example of adding a field to *all* of Mezzanine's content types:
#     (
#         "mezzanine.pages.models.Page.another_field",
#         "IntegerField", # 'django.db.models.' is implied if path is omitted.
#         (_("Another name"),),
#         {"blank": True, "default": 1},
#     ),
# )

# Setting to turn on featured images for blog posts. Defaults to False.
#
# BLOG_USE_FEATURED_IMAGE = True

# If True, the django-modeltranslation will be added to the
# INSTALLED_APPS setting.
USE_MODELTRANSLATION = False

########################
# MAIN DJANGO SETTINGS #
########################

# Hosts/domain names that are valid for this site; required if DEBUG is False
# See https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
ALLOWED_HOSTS = ["localhost", '127.0.0.1','www.ampliconrepository.org', 'ampliconrepository.org','dev.ampliconrepository.org','172.31.29.144','50.19.227.137', 'host.docker.internal', '172.31.85.178','3.92.238.157','172.31.40.50','54.164.111.51']

# Add CSRF trusted origins
CSRF_TRUSTED_ORIGINS = ['https://ampliconrepository.org','https://www.ampliconrepository.org','https://dev.ampliconrepository.org', 'http://127.0.0.1:8888/']
# skip intermediate sign-out page
ACCOUNT_LOGOUT_ON_GET = True
# SSL Redirect

SECURE_SSL_REDIRECT_ENVVAR=os.getenv('SECURE_SSL_REDIRECT', default="True")
SECURE_SSL_REDIRECT=(SECURE_SSL_REDIRECT_ENVVAR == 'True')

# Local time zone for this installation. Choices can be found here:
# http://en.wikipedia.org/wiki/List_of_tz_zones_by_name
# although not all choices may be available on all operating systems.
# On Unix systems, a value of None will cause Django to use the same
# timezone as the operating system.
# If running in a Windows environment this must be set to the same as your
# system time zone.
TIME_ZONE = "UTC"

# If you set this to True, Django will use timezone-aware datetimes.
USE_TZ = True

# Language code for this installation. All choices can be found here:
# http://www.i18nguy.com/unicode/language-identifiers.html
LANGUAGE_CODE = "en"

# Supported languages
LANGUAGES = (("en", _("English")),)

# A boolean that turns on/off debug mode. When set to ``True``, stack traces
# are displayed for error pages. Should always be set to ``False`` in
# production. Best set to ``True`` in local_settings.py
DEBUG = False

# Whether a user's session cookie expires when the Web browser is closed.
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

SITE_ID = 1
SITE_TITLE = 'AmpliconRepository'

# If you set this to False, Django will make some optimizations so as not
# to load the internationalization machinery.
USE_I18N = False

AUTHENTICATION_BACKENDS = [
    "mezzanine.core.auth_backends.MezzanineBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# turn off email authentication when a user registers an account
ACCOUNT_EMAIL_VERIFICATION = 'none'
ACCOUNT_EMAIL_REQUIRED = False

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
#ACCOUNT_AUTHENTICATED_LOGIN_REDIRECTS = os.environ['ACCOUNT_AUTHENTICATED_LOGIN_REDIRECTS']

EMAIL_HOST = 'smtp.gmail.com' #new
EMAIL_PORT = 587 #new
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', default="")  #new
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', default="")
EMAIL_HOST_USER_SECRET = EMAIL_HOST_USER
EMAIL_USE_TLS = True #new
SITE_URL = os.environ.get("SITE_URL", default="http://127.0.0.1:8000/")

SERVER_IDENTIFICATION_BANNER=os.getenv('SERVER_IDENTIFICATION_BANNER', default=None)

logging.error(f"SERVER_IDENTIFICATION_BANNER: {SERVER_IDENTIFICATION_BANNER}")

#ACCOUNT_AUTHENTICATED_LOGIN_REDIRECTS = os.environ['ACCOUNT_AUTHENTICATED_LOGIN_REDIRECTS']

SECRET_KEY = 'c4nc3r'

# The numeric mode to set newly-uploaded files to. The value should be
# a mode you'd pass directly to os.chmod.
FILE_UPLOAD_PERMISSIONS = 0o644

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Provider specific settings
GOOGLE_SECRET_KEY = os.environ['GOOGLE_SECRET_KEY']
GLOBUS_SECRET_KEY = os.environ['GLOBUS_SECRET_KEY']
ACCOUNT_DEFAULT_HTTP_PROTOCOL = os.getenv('ACCOUNT_DEFAULT_HTTP_PROTOCOL', default='https')

RECAPTCHA_PRIVATE_KEY = os.environ['RECAPTCHA_PRIVATE_KEY']
RECAPTCHA_PUBLIC_KEY =  os.environ['RECAPTCHA_PUBLIC_KEY']

# add a custom account adaptor to prevent having a username match an email in another user
# account
ACCOUNT_ADAPTER = "caper.utils.CustomAccountAdapter"
SOCIALACCOUNT_ADAPTER = 'caper.utils.SocialAccountAdapter'

SOCIALACCOUNT_EMAIL_REQUIRED=True
SOCIALACCOUNT_PROVIDERS = {
    'google': {
        # For each OAuth based provider, either add a ``SocialApp``
        # (``socialaccount`` app) containing the required client
        # credentials, or list them here:
        'SCOPE': [
            'https://www.googleapis.com/auth/userinfo.profile',
            'https://www.googleapis.com/auth/userinfo.email',
            'profile',
            'email'
        ],
        'AUTH_PARAMS': {'access_type': 'online'},
        'APP': {
            #'client_id': '789453891819-hk9q466oq5ba8i2ur4pk8d0of2f056sc.apps.googleusercontent.com',
            'client_id': '715102420712-3c11l0918iers60ca8eifnnpuihu88sm.apps.googleusercontent.com',
            'secret': GOOGLE_SECRET_KEY,
        }
    },
    'globus': {
        # For each OAuth based provider, either add a ``SocialApp``
        # (``socialaccount`` app) containing the required client
        # credentials, or list them here:
        'SCOPE': [
            'openid',
            'profile',
            'email',
            'urn:globus:auth:scope:transfer.api.globus.org:all'
        ],
        'APP': {
            'client_id': '61af67c2-8697-4afd-a5df-2a46a8ef17df',
            'secret': GLOBUS_SECRET_KEY,
        }
    }
}
SOCIAL_AUTH_GOOGLE_OAUTH2_SCOPE = [
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile'
]

ACCOUNT_FORMS = {'signup': 'caper.forms.MySignUpForm'}

#############
# DATABASES #
#############

DATABASE_ROUTERS = {
    'caper.dbrouters.RunsDBRouter'
}

DATABASES = {
    "default": {
        # Add "postgresql_psycopg2", "mysql", "sqlite3" or "oracle".
        "ENGINE": "django.db.backends.sqlite3",
        # DB name or path to database file if using sqlite3.
        "NAME": "caper.sqlite3",
    }
#     'mongo': {
#         'ENGINE': 'djongo',
#         'NAME': 'caper',
#         'ENFORCE_SCHEMA': False
#     }
}

# Formatting of numeric content on site (add commas in thousands places)
USE_THOUSAND_SEPARATOR = True

#########
# PATHS #
#########

# Full filesystem path to the project.
PROJECT_APP_PATH = os.path.dirname(os.path.abspath(__file__))
PROJECT_APP = os.path.basename(PROJECT_APP_PATH)
PROJECT_ROOT = BASE_DIR = os.path.dirname(PROJECT_APP_PATH)

# Every cache key will get prefixed with this value - here we set it to
# the name of the directory the project is in to try and use something
# project specific.
CACHE_MIDDLEWARE_KEY_PREFIX = PROJECT_APP


USE_S3 = os.getenv('S3_STATIC_FILES') == 'TRUE'

if USE_S3:
    # s3 static settings
    # URL prefix for static files.
    # Example: "http://media.lawrence.com/static/"

    STATIC_URL = "https://amprepobucket.s3.amazonaws.com/static/"
    # Absolute path to the directory static files should be collected to.
    # Don't put anything in this directory yourself; store your static files
    # in apps' "static/" subdirectories and in STATICFILES_DIRS.
    # Example: "/home/media/media.lawrence.com/static/"
    #STATIC_ROOT = os.path.join(PROJECT_ROOT, STATIC_URL.strip("/"))
    STATIC_ROOT = "/srv/static"
else:
    STATIC_URL = '/static/'
    STATIC_ROOT = os.path.join(PROJECT_ROOT, STATIC_URL.strip("/"))





# URL that handles the media served from MEDIA_ROOT. Make sure to use a
# trailing slash.
# Examples: "http://media.lawrence.com/media/", "http://example.com/media/"
MEDIA_URL = "/tmp/"

# Absolute filesystem path to the directory that will hold user-uploaded files.
# Example: "/home/media/media.lawrence.com/media/"
MEDIA_ROOT = os.path.join(PROJECT_ROOT, MEDIA_URL.strip("/"))

# Package/module name to import the root urlpatterns from for the project.
ROOT_URLCONF = "%s.urls" % PROJECT_APP

# config for uploading/downloading files directly to S3
USE_S3_DOWNLOADS = os.getenv('S3_FILE_DOWNLOADS') == 'TRUE'
if USE_S3_DOWNLOADS:
    # config for BOTO, bucket etc here but not credentials
    if os.getenv("AWS_PROFILE_NAME") is not None:
        AWS_PROFILE_NAME=os.getenv("AWS_PROFILE_NAME")
    else:
        AWS_PROFILE_NAME = 'default'

    # assume UUIDs are unique across servers so we can all use the same bucket
    S3_DOWNLOADS_BUCKET='amprepo-private'
    S3_DOWNLOADS_BUCKET_PATH=os.getenv('S3_DOWNLOADS_BUCKET_PATH', default="")



TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(PROJECT_ROOT, "templates")],
        # "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.debug",
                "django.template.context_processors.i18n",
                "django.template.context_processors.static",
                "django.template.context_processors.media",
                "django.template.context_processors.request",
                "django.template.context_processors.tz",
                "mezzanine.conf.context_processors.settings",
                "mezzanine.pages.context_processors.page",
                # 'caper.context_processors.get_files'
                "caper.context_processor.context_processor",
                "caper.context_processor.server_identification_banner",

            ],
            "loaders": [
                "mezzanine.template.loaders.host_themes.Loader",
                "django.template.loaders.filesystem.Loader",
                "django.template.loaders.app_directories.Loader",
            ],
        },
    },
]

################
# APPLICATIONS #
################

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.redirects",
    "django.contrib.sessions",
    "django.contrib.sites",
    "django.contrib.sitemaps",
    "django.contrib.messages",
    "django_recaptcha",
    # "django_extensions",
    # "sslserver",
    "django.contrib.staticfiles",
    'crispy_forms',
    'bootstrap4',
    "mezzanine.boot",
    "mezzanine.conf",
    "mezzanine.core",
    "mezzanine.generic",
    "mezzanine.pages",
    "mezzanine.forms",
    "mezzanine.galleries",
    "caper",
    # "mezzanine.twitter",
    # 'mezzanine.accounts',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.globus',
    'allauth.socialaccount.providers.google',
    'rest_framework',
    ]

CRISPY_TEMPLATE_PACK = 'bootstrap4'
# List of middleware classes to use. Order is important; in the request phase,
# these middleware classes will be applied in the order given, and in the
# response phase the middleware will be applied in reverse order.
MIDDLEWARE = (
    "mezzanine.core.middleware.UpdateCacheMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Uncomment if using internationalisation or localisation
    # 'django.middleware.locale.LocaleMiddleware',
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "mezzanine.core.request.CurrentRequestMiddleware",
    "mezzanine.core.middleware.RedirectFallbackMiddleware",
    "mezzanine.core.middleware.AdminLoginInterfaceSelectorMiddleware",
    "mezzanine.core.middleware.SitePermissionMiddleware",
    "mezzanine.pages.middleware.PageMiddleware",
    "mezzanine.core.middleware.FetchFromCacheMiddleware",
)

# Store these package names here as they may change in the future since
# at the moment we are using custom forks of them.
PACKAGE_NAME_FILEBROWSER = "filebrowser_safe"
PACKAGE_NAME_GRAPPELLI = "grappelli_safe"

#########################
# OPTIONAL APPLICATIONS #
#########################

# These will be added to ``INSTALLED_APPS``, only if available.
OPTIONAL_APPS = (
    "debug_toolbar",
    "django_extensions",
    "compressor",
    PACKAGE_NAME_FILEBROWSER,
    PACKAGE_NAME_GRAPPELLI,
)

##################
# LOCAL SETTINGS #
##################

# Allow any settings to be defined in local_settings.py which should be
# ignored in your version control system allowing for settings to be
# defined per machine.

# Instead of doing "from .local_settings import *", we use exec so that
# local_settings has full access to everything defined in this module.
# Also force into sys.modules so it's visible to Django's autoreload.

f = os.path.join(PROJECT_APP_PATH, "local_settings.py")
if os.path.exists(f):
    import imp
    import sys

    module_name = "%s.local_settings" % PROJECT_APP
    module = imp.new_module(module_name)
    module.__file__ = f
    sys.modules[module_name] = module
    exec(open(f, "rb").read())


PROJECT_DATA_URL='/project_data/'
PROJECT_DATA_ROOT=os.path.join(BASE_DIR,'project_data')

ACCOUNT_FORMS = {'signup': 'caper.forms.MySignUpForm'}
SOCIALACCOUNT_FORMS =  {'signup': 'caper.forms.MySocialSignUpForm'}
SOCIALACCOUNT_LOGIN_ON_GET = True

####################
# DYNAMIC SETTINGS #
####################

# set_dynamic_settings() will rewrite globals based on what has been defined so far, in
# order to provide some better defaults where applicable.
try:
    from mezzanine.utils.conf import set_dynamic_settings
except ImportError:
    pass
else:
    set_dynamic_settings(globals())

DATA_UPLOAD_MAX_MEMORY_SIZE = 524288000
DATA_UPLOAD_MAX_NUMBER_FIELDS = None

###########################
# version info for footer #
###########################

SERVER_VERSION = "unversioned"
with open("version.txt", 'r') as version_file:
    comment_char = "#"
    sep = "="
    for line in version_file:
        l = line.strip()
        if l and not l.startswith(comment_char):
            key_value = l.split(sep)
            key = key_value[0].strip()
            value = sep.join(key_value[1:]).strip().strip('"')
            if "version" == key.lower():
                SERVER_VERSION = value
                break

