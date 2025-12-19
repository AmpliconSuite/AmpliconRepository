from django.conf import settings

# In-memory flag for shutdown mode (resets on server restart)
_shutdown_pending = False

# In-memory flag for disabling user registration (resets on server restart)
_registration_disabled = False


def set_shutdown_pending(status):
    """Set the shutdown pending flag"""
    global _shutdown_pending
    _shutdown_pending = status


def get_shutdown_pending():
    """Get the shutdown pending flag"""
    global _shutdown_pending
    return _shutdown_pending


def set_registration_disabled(status):
    """Set the registration disabled flag"""
    global _registration_disabled
    _registration_disabled = status


def get_registration_disabled():
    """Get the registration disabled flag"""
    global _registration_disabled
    return _registration_disabled


def context_processor(request):
    context = {}
    context['SERVER_VERSION'] = settings.SERVER_VERSION
    return context   


def server_identification_banner(request):
    return {
        'SERVER_IDENTIFICATION_BANNER': settings.SERVER_IDENTIFICATION_BANNER
    }


def shutdown_mode(request):
    """Context processor to make shutdown mode available to all templates"""
    return {
        'SHUTDOWN_PENDING': get_shutdown_pending()
    }


def registration_mode(request):
    """Context processor to make registration disabled mode available to all templates"""
    return {
        'REGISTRATION_DISABLED': get_registration_disabled()
    }

