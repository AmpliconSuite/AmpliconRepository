from django import forms

from .models import Run

class RunForm(forms.ModelForm):
    class Meta:
        model = Run
        fields = ('project_name','description','private','project_members', 'file')

class UpdateForm(forms.ModelForm):
    class Meta:
        model = Run
        fields = ('description','private','project_members', 'file')
    
    def __init__(self, *args, **kwargs):
        super(UpdateForm, self).__init__(*args, **kwargs)
        self.fields['description'].required = False
        self.fields['private'].required = False
        self.fields['project_members'].required = False
        self.fields['file'].required = False

# class ViewRunForm(forms.ModelForm):
#     class Meta:
#         model = Run
#         fields = ('project_name','file')