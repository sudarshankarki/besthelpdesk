from urllib.parse import urlencode

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.contrib import messages
from django.conf import settings
from django.core.mail import send_mail
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.contrib.auth import views as auth_views
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
import logging
from .forms import CompleteSignupForm, SignupRequestForm
from .utils import get_outgoing_from_email, logout_user_from_all_sessions
from tickets.models import Ticket

logger = logging.getLogger(__name__)

def signup(request):
    if request.method == 'POST':
        form = SignupRequestForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"]
            signer = TimestampSigner(salt="bestsupport-signup")
            signed_email = signer.sign(email)
            complete_url = request.build_absolute_uri(
                reverse("complete_signup", kwargs={"token": signed_email})
            )

            try:
                send_mail(
                    subject="Complete your BestSupport registration",
                    message=(
                        "Hello,\n\n"
                        "Please complete your registration by opening this link:\n"
                        f"{complete_url}\n\n"
                        "If you did not request this, you can ignore this email."
                    ),
                    from_email=get_outgoing_from_email(),
                    recipient_list=[email],
                    fail_silently=False,
                )
                return render(request, "accounts/verification_sent.html", {"email": email})
            except Exception:
                logger.exception("Failed to send signup email to %s", email)
                messages.error(
                    request,
                    "Could not send the registration email. Please contact IT Support.",
                )
        messages.error(request, 'Please correct the errors below.')
    else:
        form = SignupRequestForm()

    return render(request, 'accounts/signup.html', {'form': form})


def complete_signup(request, token):
    signer = TimestampSigner(salt="bestsupport-signup")
    try:
        email = signer.unsign(token, max_age=settings.SIGNUP_LINK_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return render(request, "accounts/verification_invalid.html")

    if request.method == "POST":
        form = CompleteSignupForm(request.POST, email=email)
        if form.is_valid():
            form.save()
            messages.success(request, "Account created successfully. You can now log in.")
            return redirect("login")
        messages.error(request, "Please correct the errors below.")
    else:
        form = CompleteSignupForm(email=email)

    return render(request, "accounts/complete_signup.html", {"form": form, "email": email})


def login_view(request):
    if request.method == 'POST':
        username_or_email = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''

        login_username = username_or_email
        if '@' in username_or_email:
            user_model = get_user_model()
            matched_user = user_model.objects.filter(email__iexact=username_or_email).first()
            if matched_user:
                login_username = matched_user.get_username()

        user = authenticate(request, username=login_username, password=password)

        if user is not None:
            login(request, user)
            messages.success(request, f'Welcome back, {user.get_username()}!')
            return redirect('ticket_list')
        else:
            user_model = get_user_model()
            candidate = user_model.objects.filter(username=login_username).first()
            if candidate and candidate.check_password(password) and not candidate.is_active:
                messages.warning(request, 'Please verify your email before logging in.')
                return render(request, 'accounts/login.html')
            messages.error(request, 'Invalid username/email or password.')

    return render(request, 'accounts/login.html')


class PasswordResetView(auth_views.PasswordResetView):
    def form_valid(self, form):
        self.from_email = get_outgoing_from_email()
        return super().form_valid(form)


def verify_email(request, uidb64, token):
    user_model = get_user_model()
    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
        user = user_model.objects.get(pk=user_id)
    except (TypeError, ValueError, OverflowError, user_model.DoesNotExist):
        user = None

    if user is not None and default_token_generator.check_token(user, token):
        user.email_verified = True
        user.is_active = True
        user.save(update_fields=["email_verified", "is_active"])
        messages.success(request, "Email verified successfully. You can now log in.")
        return redirect("login")

    return render(request, "accounts/verification_invalid.html")


def logout_view(request):
    if request.user.is_authenticated:
        logout_user_from_all_sessions(request.user)
    logout(request)
    messages.success(request, 'You have been successfully logged out.')
    return redirect('login')


@login_required
def dashboard(request):
    user = request.user

    user_ticket_queryset = Ticket.objects.filter(created_by=user)
    user_tickets = user_ticket_queryset.order_by('-created_at')[:5]

    new_tickets = user_ticket_queryset.filter(status="new").count()
    in_progress_tickets = user_ticket_queryset.filter(status='in_progress').count()
    resolved_tickets = user_ticket_queryset.filter(status='resolved').count()
    closed_tickets = user_ticket_queryset.filter(status='closed').count()

    def _ticket_list_url(**params):
        base_url = reverse("ticket_list")
        query = urlencode({key: value for key, value in params.items() if value not in {"", None}})
        return f"{base_url}?{query}" if query else base_url

    context = {
        'user_tickets': user_tickets,
        "new_tickets": new_tickets,
        'in_progress_tickets': in_progress_tickets,
        'resolved_tickets': resolved_tickets,
        "closed_tickets": closed_tickets,
        "total_tickets": user_ticket_queryset.count(),
        "all_tickets_url": _ticket_list_url(scope="created_by_me"),
        "new_tickets_url": _ticket_list_url(status="new", scope="created_by_me"),
        "in_progress_tickets_url": _ticket_list_url(status="in_progress", scope="created_by_me"),
        "resolved_tickets_url": _ticket_list_url(status="resolved", scope="created_by_me"),
        "closed_tickets_url": _ticket_list_url(status="closed", scope="created_by_me"),
    }

    return render(request, 'accounts/dashboard.html', context)


def home(request):
    return redirect('login')
