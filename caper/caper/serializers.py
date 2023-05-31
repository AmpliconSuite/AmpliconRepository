from rest_framework import serializers
from .models import File


class FileSerializer(serializers.ModelSerializer):
    """
    Serializer for upload files
    """
    class Meta:
        model = File
        fields = "__all__"