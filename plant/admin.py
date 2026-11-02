from django.contrib import admin

from .models import MO, CostItem, Machine, PartSpec, Person, PlantSettings, Shift, ShiftBreak


class BreakInline(admin.TabularInline):
    model = ShiftBreak
    extra = 0


@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ("name", "start", "end", "days")
    inlines = [BreakInline]


@admin.register(Machine)
class MachineAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "stage", "speed_mode", "ppm", "rate", "active", "feed_type")
    list_filter = ("stage", "active")


@admin.register(MO)
class MOAdmin(admin.ModelAdmin):
    list_display = ("number", "part", "qty", "due", "status", "line", "seq", "produced", "scrap")
    list_filter = ("status", "line", "source")
    search_fields = ("number", "part")


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("name", "role", "active", "source")
    list_filter = ("role", "active", "source")
    filter_horizontal = ("trained",)


admin.site.register(PartSpec)
admin.site.register(CostItem, list_display=("aq", "cust", "cat", "pleats", "ppm", "nppm"), search_fields=("aq", "cust"))
admin.site.register(PlantSettings)
admin.site.site_header = "Production Suite"
