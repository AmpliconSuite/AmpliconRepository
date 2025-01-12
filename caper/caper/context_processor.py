from django.conf import settings

def context_processor(request):
    context = {}
    context['SERVER_VERSION'] = settings.SERVER_VERSION
    return context   


def server_identification_banner(request):
    return {
        'SERVER_IDENTIFICATION_BANNER': settings.SERVER_IDENTIFICATION_BANNER
    }