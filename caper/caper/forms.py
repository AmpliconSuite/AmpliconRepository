from django import forms

from .models import Run
from .models import FeaturedProjectUpdate
from allauth.account.forms import SignupForm
from allauth.socialaccount.forms import SignupForm as SocialSignupForm

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Submit, Layout, Field


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
