from django.db import models
import uuid
import datetime

class Run(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project_name = models.CharField(max_length=1000)
    description = models.CharField(max_length=1000)
    publication_link = models.CharField(max_length=1000, blank=True)
    #private = models.BooleanField(default=True)
    BOOL_CHOICES = ((True, 'Private'), (False, 'Public'))
    private = models.BooleanField(choices=BOOL_CHOICES,default=True)
    project_members = models.CharField(max_length=1000, blank = True)


class FeaturedProjectUpdate(models.Model):
    project_name = models.CharField(max_length=1000)
    project_id = models.CharField(max_length=1000)
    featured = models.BooleanField(default=False)

class AdminDeleteProject(models.Model):
    project_name = models.CharField(max_length=1000)
    project_id = models.CharField(max_length=1000)
    action = models.CharField(max_length=20)

    delete = models.BooleanField(default=False)

class AdminSendEmail(models.Model):
    to = models.CharField(max_length=200)
    cc = models.CharField(max_length=1000, blank=True)
    subject = models.CharField(max_length=200)
    body = models.CharField(max_length=4000)



class UploadTarFile(models.Model):
    """
    Model for tarfile upload directly from AmpliconSuiteAggregator
    """
    id = models.UUIDField(primary_key=True, default = uuid.uuid4, editable = False)
    file = models.FileField(blank = False, null = True)

    def __str__(self):
        return self.file.name

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
