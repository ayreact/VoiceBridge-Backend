from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    phone = models.CharField(max_length=20, blank=True, null=True)
    language = models.CharField(max_length=20, default="en")
    device_type = models.CharField(max_length=20, default="smartphone")

    def __str__(self):
        return f"{self.user.username}'s Profile"


class QueryHistory(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    query = models.TextField()
    response = models.TextField()
    category = models.CharField(max_length=50)
    language = models.CharField(max_length=20)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.category} @ {self.timestamp}"


class LessonContent(models.Model):
    title = models.CharField(max_length=100)
    category = models.CharField(max_length=50)
    language = models.CharField(max_length=10)
    body = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.title} ({self.language} - {self.category})"
