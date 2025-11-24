from django.conf import settings

# In-memory flag for shutdown mode (resets on server restart)
_shutdown_pending = False


def set_shutdown_pending(status):
    """Set the shutdown pending flag"""
    global _shutdown_pending
    _shutdown_pending = status


def get_shutdown_pending():
    """Get the shutdown pending flag"""
    global _shutdown_pending
    return _shutdown_pending


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
