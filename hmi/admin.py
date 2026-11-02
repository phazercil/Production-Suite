from django.contrib import admin

from . import models as m

for model, cols in ((m.ShiftRun, ("line", "user_name", "start", "end", "active", "count")), (m.Downtime, ("line", "start_time", "minutes", "reason")),
                    (m.QCCheck, ("line", "t", "item", "passed")), (m.Ticket, ("machine", "type", "priority", "status", "t")),
                    (m.MoRun, ("line", "mo", "item", "qty", "start_time")), (m.AuditLog, ("t", "by", "what")), (m.SavedQuery, ("name", "saved_by", "saved_at"))):
    admin.site.register(model, list_display=cols)
