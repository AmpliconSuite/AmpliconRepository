from .models import Run
    
class RunsDBRouter:
    def db_for_read (self, model, **hints):
        if (model == Run):
            # your model name as in settings.py/DATABASES
            return 'mongo'
        return None
    
    def db_for_write (self, model, **hints):
        if (model == Run):
            # your model name as in settings.py/DATABASES
            return 'mongo'
        return None