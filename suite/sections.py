"""Sections of the suite that are coming later. Their place in the top bar, address and permission are set now."""
from django.shortcuts import render

from plant import perms

SOON = {}   # the Packaging Kiosk was here; it is its own section now (packkiosk/)


def soon(request, key):
    title, about, perm = SOON[key]
    return render(request, "sections/soon.html", {"mode": key, "title": title, "about": about, "perm_label": perms.PERMS[perm]["label"]})
