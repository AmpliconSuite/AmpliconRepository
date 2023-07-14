from django.conf import settings

def context_processor(request):
    context = {}
    context['SERVER_VERSION'] = settings.SERVER_VERSION
    return context   


