from django import forms
from django.utils.html import format_html

from .models import Run
from .models import FeaturedProjectUpdate, AdminDeleteProject
from allauth.account.forms import SignupForm
from allauth.socialaccount.forms import SignupForm as SocialSignupForm

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Submit, Layout, Field


class RunForm(forms.ModelForm):
    accept_license = forms.BooleanField(
        label='I acknowledge and accept the terms of the license agreement',
        required=True,
        widget=forms.CheckboxInput(),
        help_text=format_html(
            "Data contributed to AmpliconRepository is licensed under the <a href='https://raw.githubusercontent.com/AmpliconSuite/AmpliconRepository/main/licenses/CCv4-BY.txt'>Creative Commons v4 license</a>."
        ),
    )

    class Meta:
        model = Run
        fields = ('project_name','description','private','project_members', 'accept_license')
        labels = {
            'private': 'Visibility'
        }

class UpdateForm(forms.ModelForm):
    accept_license = forms.BooleanField(
        label='I acknowledge and accept the terms of the license agreement',
        required=True,
        widget=forms.CheckboxInput(),
        help_text=format_html(
                        "Data contributed to AmpliconRepository is licensed under the <a href='https://raw.githubusercontent.com/AmpliconSuite/AmpliconRepository/main/licenses/CCv4-BY.txt'>Creative Commons v4 license</a>."
        ),
    )

    class Meta:
        model = Run
        fields = ('project_name', 'description','private','project_members', 'accept_license')
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

class DeletedProjectForm(forms.ModelForm):
    class Meta:
        model = AdminDeleteProject
        fields = ('project_name','project_id','delete', 'action')



class MySignUpForm(SignupForm):

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
