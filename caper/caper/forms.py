from django import forms

from .models import Run
from .models import FeaturedProjectUpdate

class RunForm(forms.ModelForm):
    class Meta:
        model = Run
        fields = ('project_name','description','private','project_members')
        labels = {
            'private': 'Visibility'
        }

class UpdateForm(forms.ModelForm):
    class Meta:
        model = Run
        fields = ('description','private','project_members')
        labels = {
            'private': 'Visibility'
        }
    
    def __init__(self, *args, **kwargs):
        super(UpdateForm, self).__init__(*args, **kwargs)
        self.fields['description'].required = False
        self.fields['private'].required = False

        self.fields['project_members'].required = False
        # self.fields['file'].required = False


class FeaturedProjectForm(forms.ModelForm):
    class Meta:
        model = FeaturedProjectUpdate
        fields = ('project_name','project_id','featured')





# class RunForm(forms.ModelForm):
#     class Meta:
#         model = Run
#         fields = ('project_name','description','private','project_members')

# class ViewRunForm(forms.ModelForm):
#     class Meta:
#         model = Run
#         fields = ('project_name','file')