from django import forms
from django.utils.html import format_html

from .models import Run
from .models import FeaturedProjectUpdate, AdminDeleteProject, AdminSendEmail, UserPreferencesModel, UploadTarFile
from allauth.account.forms import SignupForm
from allauth.socialaccount.forms import SignupForm as SocialSignupForm
from django_recaptcha.widgets import ReCaptchaV2Checkbox
from django_recaptcha.fields import ReCaptchaField
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Submit, Layout, Field

from django.utils.safestring import mark_safe


class RunForm(forms.ModelForm):
    accept_license = forms.BooleanField(
        label=format_html(
            "Data contributed to AmpliconRepository is licensed under the <a href='https://raw.githubusercontent.com/AmpliconSuite/AmpliconRepository/main/licenses/CCv4-BY.txt'>Creative Commons v4 license</a>."),
        required=True,
        widget=forms.CheckboxInput(),
        help_text=
            'Click checkbox to acknowledge and accept the terms of the license agreement.',
    )

    class Meta:
        model = Run
        fields = ('project_name','description','publication_link','private','project_members', 'accept_license', 'alias')
        labels = {
            'private': 'Visibility'
        }
        help_texts = {
            'private': format_html('&nbsp;<b>Private</b>: Only you and project members can view the project.<br>&nbsp;<b>Public</b>: Anyone may view the project.<br>&nbsp;Only you and project members may edit the project. Visibility settings can be updated later.'),
        }

    def __init__(self, *args, **kwargs):
        super(RunForm, self).__init__(*args, **kwargs)
        self.fields['publication_link'].widget.attrs.update({'placeholder': 'Optional: Provide a PMID or link to a publication'})
        self.fields['project_members'].widget.attrs.update({'placeholder': 'Optional: List of additional email addresses or AmpliconRepository usernames separated by spaces or commas'})
        self.helper = FormHelper()

class UpdateForm(forms.ModelForm):
    
    accept_license = forms.BooleanField(
        label=format_html(
            "I acknowledge that the uploaded files will be released under a <a href='https://raw.githubusercontent.com/AmpliconSuite/AmpliconRepository/main/licenses/CCv4-BY.txt'>Creative Commons v4 license</a>."),
        required=True,
        widget=forms.CheckboxInput(),
        help_text=
        'Click checkbox to acknowledge and accept the terms of the license agreement.',
    )
    
    replace_project = forms.BooleanField(
        label = format_html(
            "Replace Project? If ticked, will replace the entire project with the file you upload."
        ), 
        required = False, 
        widget = forms.CheckboxInput(),
        help_text = 'The default behavior is to add samples to the current project.'
    )

    class Meta:
        model = Run
        fields = ('project_name', 'description', 'publication_link', 'private', 'project_members', 'accept_license', 'alias')
        labels = {
            'private': 'Visibility'
        }
        help_texts = {
            'private': format_html(
                '&nbsp;<b>Private</b>: Only you and project members can view the project.<br>&nbsp;<b>Public</b>: Anyone may view the project.<br>&nbsp;Only you and project members may edit the project.'),
        }
    
    def __init__(self, *args, **kwargs):
        super(UpdateForm, self).__init__(*args, **kwargs)
        self.fields['description'].required = False
        self.fields['private'].required = False
        self.fields['publication_link'].required = False
        self.fields['publication_link'].widget.attrs.update({'placeholder': 'Optional: Provide a PMID or link to a publication'})
        self.fields['project_members'].required = False
        self.fields['project_members'].widget.attrs.update({'placeholder': 'Optional: List of additional email addresses or AmpliconRepository usernames separated by spaces or commas'})
        self.fields['replace_project'].widget.attrs.update({'id': 'custom_id_replace_project'})
        # self.fields['file'].required = False


class FeaturedProjectForm(forms.ModelForm):
    class Meta:
        model = FeaturedProjectUpdate
        fields = ('project_name','project_id','featured')

class DeletedProjectForm(forms.ModelForm):
    class Meta:
        model = AdminDeleteProject
        fields = ('project_name','project_id','delete', 'action')

class SendEmailForm(forms.ModelForm):

    class Meta:
        model = AdminSendEmail
        fields = ('to','cc','subject', 'body')

class UserPreferencesForm(forms.ModelForm):

    class Meta:
        model = UserPreferencesModel
        fields = ('onAddedToProjectTeam', 'onRemovedFromProjectTeam')


class MySignUpForm(SignupForm):
    captcha = ReCaptchaField(widget=ReCaptchaV2Checkbox)
    def __init__(self, *args, **kwargs):
        super(MySignUpForm, self).__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_id = 'signup_form'
        self.helper.form_class = 'signup'
        self.helper.form_method = 'post'
        #self.helper.form_action = reverse('thankyou')
        # and then the rest as usual:
        self.helper.form_show_labels = True
        self.helper.add_input(Submit('signup', 'Create My Account'))

class MySocialSignUpForm(SocialSignupForm):

    def __init__(self, *args, **kwargs):
        super(MySocialSignUpForm, self).__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_id = 'signup_form'
        self.helper.form_class = 'signup'
        self.helper.form_method = 'post'
        #self.helper.form_action = reverse('thankyou')
        # and then the rest as usual:
        self.helper.form_show_labels = True
        self.helper.add_input(Submit('signup', 'Create My Account'))
# class RunForm(forms.ModelForm):
#     class Meta:
#         model = Run
#         fields = ('project_name','description','private','project_members')

# class ViewRunForm(forms.ModelForm):
#     class Meta:
#         model = Run
#         fields = ('project_name','file')
