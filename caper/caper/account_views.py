from allauth.account.views import PasswordChangeView
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


class LocalPasswordChangeView(PasswordChangeView):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.has_usable_password():
            messages.info(
                request,
                "This account uses Google or Globus sign-in and does not have "
                "a local password to change.",
            )
            return redirect('profile')
        return super().dispatch(request, *args, **kwargs)


password_change = login_required(LocalPasswordChangeView.as_view())


@login_required
def password_set_unavailable(request):
    messages.info(
        request,
        "This account uses Google or Globus sign-in and does not need a "
        "local password.",
    )
    return redirect('profile')
