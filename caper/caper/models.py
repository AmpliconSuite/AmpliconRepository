from django.db import models
import uuid
import datetime

class Run(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project_name = models.CharField(max_length=1000)
    description = models.CharField(max_length=1000)
    private = models.BooleanField(default=False)
    project_members = models.CharField(max_length=1000)

# class Feature(models.Model):
#     id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
#     project_name = models.CharField(max_length=1000)
#     sample_name = models.CharField(max_length=1000)
#     feature_name = models.CharField(max_length=1000)
#     cnv = models.CharField(max_length=1000)
#     png = models.CharField(max_length=1000)
#     pdf = models.CharField(max_length=1000)

        
# class Project(models.Model):
#     id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
#     name = models.CharField(max_length=100)
#     file = models.FileField()
