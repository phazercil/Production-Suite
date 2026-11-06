"""Sections of the suite that are coming later. Their place in the top bar, address and permission are set now."""
from django.shortcuts import render

from plant import perms

SOON = {
    "maintenance": ("Maintenance", "Maintenance's own view of the plant: requests from the lines, repairs and their notes, and planned work.",
                    "section.maintenance"),
}


def soon(request, key):
    title, about, perm = SOON[key]
    return render(request, "sections/soon.html", {"mode": key, "title": title, "about": about, "perm_label": perms.PERMS[perm]["label"]})
