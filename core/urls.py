from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from .views import (
    RegisterView,
    CustomTokenObtainPairView,
    UserProfileView,
    QueryHistoryList,
    AssistantQueryView,
    VoiceUploadView,
    LessonContentView,
    IVRHookView,
    WhatsAppWebhookView,
)

urlpatterns = [
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/login/", CustomTokenObtainPairView.as_view(), name="register"),
    path("user/profile/", UserProfileView.as_view(), name="user-profile"),
    path("logs/query-history", QueryHistoryList.as_view(), name="query-history"),
    path("assistant/query", AssistantQueryView.as_view(), name="assistant-query"),
    path("assistant/voice-upload", VoiceUploadView.as_view(), name="voice-upload"),
    path("assistant/topic-lessons", LessonContentView.as_view(), name="lesson-content"),
    path("assistant/ivr-hook", csrf_exempt(IVRHookView.as_view()), name="ivr-hook"),
    path("assistant/whatsapp-hook", csrf_exempt(WhatsAppWebhookView.as_view()), name="whatsapp-webhook"),
]