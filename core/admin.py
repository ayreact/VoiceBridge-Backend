from django.contrib import admin
from .models import UserProfile, QueryHistory, LessonContent

@admin.register(LessonContent)
class LessonContentAdmin(admin.ModelAdmin):
    list_display = ('title', 'category', 'language', 'short_body')
    search_fields = ('title', 'category', 'language', 'body')
    list_filter = ('category', 'language')
    ordering = ('title',)
    readonly_fields = ()  # You can set fields here if you want to make any non-editable

    def short_body(self, obj):
        return obj.body[:75] + '...' if len(obj.body) > 75 else obj.body
    short_body.short_description = 'Body Preview'
    
admin.site.register(UserProfile)
admin.site.register(QueryHistory)
