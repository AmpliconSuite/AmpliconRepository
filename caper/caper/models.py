from django.db import models
import uuid
import datetime

class Run(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project_name = models.CharField(max_length=1000)
    description = models.CharField(max_length=1000)
    private = models.BooleanField(default=False)
    project_members = models.CharField(max_length=1000)
        
# class Project(models.Model):
#     id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
#     name = models.CharField(max_length=100)
#     file = models.FileField()
