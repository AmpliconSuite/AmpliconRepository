from rest_framework import serializers
from .models import UploadTarFile


class FileSerializer(serializers.ModelSerializer):
    """
    Serializer for upload files
    """
    class Meta:
        model = UploadTarFile
        fields = "__all__"