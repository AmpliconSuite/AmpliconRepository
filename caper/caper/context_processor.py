import datetime

from django.conf import settings


def _get_settings_collection():
    """Get or create the settings collection handle"""
    from .utils import db_handle
    return db_handle['system_settings']


def set_shutdown_pending(status):
    """Set the shutdown pending flag in database"""
    settings_collection = _get_settings_collection()
    settings_collection.update_one(
        {'_id': 'system_flags'},
        {'$set': {'shutdown_pending': status}},
        upsert=True
    )


def get_shutdown_pending():
    """Get the shutdown pending flag from database"""
    settings_collection = _get_settings_collection()
    doc = settings_collection.find_one({'_id': 'system_flags'})
    if doc and 'shutdown_pending' in doc:
        return doc['shutdown_pending']
    return False


def set_registration_disabled(status):
    """Set the registration disabled flag in database"""
    settings_collection = _get_settings_collection()
    settings_collection.update_one(
        {'_id': 'system_flags'},
        {'$set': {'registration_disabled': status}},
        upsert=True
    )


def get_registration_disabled():
    """Get the registration disabled flag from database"""
    settings_collection = _get_settings_collection()
    doc = settings_collection.find_one({'_id': 'system_flags'})
    if doc and 'registration_disabled' in doc:
        return doc['registration_disabled']
    return False


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


def ops_lock_notice(request):
    """Who holds the operator lock, for the line on admin pages.

    The read happens only for staff on /admin pages, so other requests pay nothing.
    The site never enforces the lock: this line is how someone in a browser finds out.
    """
    user = getattr(request, 'user', None)
    if not (request.path.startswith('/admin') and user is not None and user.is_staff):
        return {}
    doc = _get_settings_collection().find_one({'_id': 'ops_lock'})
    if not doc or not doc.get('holder'):
        return {}
    now = datetime.datetime.utcnow()
    if doc['expires_at'] <= now:
        return {}
    return {'OPS_LOCK': {
        'holder': doc['holder'],
        'owner': doc.get('owner'),
        'reason': doc.get('reason'),
        'minutes_held': int((now - doc['acquired_at']).total_seconds() // 60),
        'minutes_left': int((doc['expires_at'] - now).total_seconds() // 60),
    }}


def registration_mode(request):
    """Context processor to make registration disabled mode available to all templates"""
    return {
        'REGISTRATION_DISABLED': get_registration_disabled()
    }

