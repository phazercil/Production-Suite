from django.contrib import admin

from .models import Job

admin.site.register(Job, list_display=("mo", "op", "machine", "req_start"), list_filter=("op", "machine"))
