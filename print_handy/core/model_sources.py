"""The one registry of model sources Print Handy can take a link from.

Every surface that names the supported sites reads this module: fresh
link recognition (:mod:`print_handy.core.link_intake`), the per-site
inventory/download dispatch (:mod:`print_handy.core.model_job`), the
``/print-source`` report (:mod:`print_handy.core.site_accounts`), the
design fetch dispatch (:mod:`print_handy.core.design`) and the Web UI
Sources page. Before this module the list was copied into each of those
places and the copies had already drifted (different orders, a site in
one list and missing from another); an entry here is the only place a
site is added or renamed.

A source is a *site* (a model page on a known host) or one of the two
pseudo sources: ``direct`` (a public link straight at a ``.stl``/``.3mf``
file, on any host) and ``grabcad`` (known, and deliberately not
supported). Credential details do NOT live here — which file or variable
holds a site's token stays with :mod:`print_handy.core.site_accounts`;
this registry only says the site exists, what it is called, which hosts
and model-page paths are its, and which ``model_job`` functions serve it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSource:
    """One model source.

    ``kind`` is the /print-source vocabulary: ``login`` (a saved
    credential is needed to download), ``open`` (no credential),
    ``listing`` (the site can be listed but its files cannot be
    downloaded), ``none`` (not supported) and ``direct`` (the direct
    file pseudo source). ``link_pattern`` is the model-page path shape
    fresh intake accepts, matched with ``link_flags``; ``None`` means
    the source never matches a pasted site link (direct, grabcad).
    ``inventory_func``/``download_func`` are ``model_job`` function
    NAMES, resolved through that module's globals at call time so tests
    that monkeypatch the per-site functions by name keep working.
    """

    site: str
    label: str
    hosts: tuple = ()
    link_pattern: str | None = None
    link_flags: int = 0
    kind: str = "open"
    note: str = ""
    inventory_func: str | None = None
    download_func: str | None = None


# In the order people meet them (the /print-source report and the Web UI
# Sources page). SUPPORTED_LINKS_MESSAGE keeps its own historical label
# order via MESSAGE_ORDER below.
SOURCES = (
    ModelSource(
        "makerworld", "MakerWorld",
        hosts=("makerworld.com", "www.makerworld.com"),
        link_pattern=r"^/en/models/",
        kind="login",
        inventory_func="maker_inventory", download_func="maker_download"),
    ModelSource(
        "printables", "Printables",
        hosts=("printables.com", "www.printables.com"),
        link_pattern=r"^/(?:[a-z]{2}/)?model/",
        kind="open", note="no login needed",
        inventory_func="printables_inventory", download_func="printables_download"),
    ModelSource(
        "thingiverse", "Thingiverse",
        hosts=("thingiverse.com", "www.thingiverse.com"),
        link_pattern=r"^/thing:\d+",
        kind="login",
        inventory_func="thingiverse_inventory", download_func="thingiverse_download"),
    ModelSource(
        "myminifactory", "MyMiniFactory",
        hosts=("myminifactory.com", "www.myminifactory.com"),
        link_pattern=r"^/object/(?:[^/]*-)?\d+/?$",
        kind="login",
        inventory_func="myminifactory_inventory", download_func="myminifactory_download"),
    ModelSource(
        "crealitycloud", "Creality Cloud",
        hosts=("crealitycloud.com", "www.crealitycloud.com"),
        link_pattern=r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?model-detail/[^/]+/?$",
        link_flags=re.I,
        kind="login",
        inventory_func="crealitycloud_inventory", download_func="crealitycloud_download"),
    ModelSource(
        "thangs", "Thangs",
        hosts=("thangs.com", "www.thangs.com"),
        link_pattern=r"(?:^/m/\d+/?$|/3d-model/[^/]*-\d+/?$)",
        kind="login",
        inventory_func="thangs_inventory", download_func="thangs_download"),
    ModelSource(
        "nexprint", "Nexprint",
        hosts=("nexprint.com", "www.nexprint.com"),
        link_pattern=r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?models/[A-Za-z0-9]+/?$",
        link_flags=re.I,
        kind="login",
        inventory_func="nexprint_inventory", download_func="nexprint_download"),
    ModelSource(
        "cults3d", "Cults3D",
        hosts=("cults3d.com", "www.cults3d.com"),
        link_pattern=r"^/(?:[a-z]{2}/)?3d-model/(?:[^/]+/)?[^/]+/?$",
        kind="listing",
        note="listing only — Cults' API never shares a model's files",
        inventory_func="cults_inventory", download_func="cults_download"),
    ModelSource(
        "makeronline", "MakerOnline",
        hosts=("makeronline.com", "www.makeronline.com"),
        link_pattern=r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?model/(?:[^/]+/)?\d+\.html$",
        link_flags=re.I,
        kind="listing",
        note="listing only — its downloads need a browser security check "
             "Print Handy won't fake",
        inventory_func="makeronline_inventory", download_func="makeronline_download"),
    ModelSource(
        "grabcad", "GrabCAD",
        hosts=("grabcad.com", "www.grabcad.com"),
        kind="none",
        note="not supported — its firewall blocks apps and its terms forbid them"),
)

#: The direct-file pseudo source: a public link straight at a print
#: file, on any host (Dropbox share links included; see link_intake).
DIRECT = ModelSource(
    "direct", "Direct file link", kind="direct",
    note="a public link straight at a .stl or .3mf file, on any host — "
         "a Dropbox share link works too",
    inventory_func="direct_inventory", download_func="direct_download")

#: Every entry, sites first, then the direct pseudo source.
ALL = SOURCES + (DIRECT,)

#: Extensions a direct file link (and an uploaded file) may carry.
DIRECT_FILE_EXTS = frozenset({".stl", ".3mf"})

#: Label order of the operator-facing "supported links" sentence; it
#: predates the report order and is pinned by intake error text.
MESSAGE_ORDER = ("makerworld", "printables", "thingiverse", "myminifactory",
                 "cults3d", "crealitycloud", "thangs", "nexprint", "makeronline")

_BY_SITE = {source.site: source for source in ALL}
_BY_HOST = {host: source for source in ALL for host in source.hosts}


def by_site(site):
    """The registry entry for a site id, or None."""
    return _BY_SITE.get(str(site or ""))


def by_host(host):
    """The registry entry owning a hostname (bare or www), or None."""
    return _BY_HOST.get(str(host or "").lower())


def site_for_host(host):
    """The site id owning a hostname, or None."""
    source = by_host(host)
    return source.site if source else None


def hosts_map():
    """hostname -> site id, the model_job SITE_HOSTS shape.

    Only sources model_job can actually serve are included: the
    not-supported entry (GrabCAD) stays out, so a GrabCAD link falls
    through to the direct-link check and the supported-links refusal
    exactly as before this registry existed.
    """
    return {host: source.site for source in ALL if source.inventory_func
            for host in source.hosts}


def link_source(url_path_host):
    """The site whose model-page pattern matches (host, path), or None.

    ``url_path_host`` is a ``(host, path)`` pair as link intake splits
    them; matching is by the host's registry entry only, exactly the
    per-site checks fresh intake used to spell out one by one.
    """
    host, path = url_path_host
    source = by_host(host)
    if source is None or source.link_pattern is None:
        return None
    if re.search(source.link_pattern, path, source.link_flags):
        return source
    return None


def inventory_funcs():
    """site id -> model_job inventory function name."""
    return {s.site: s.inventory_func for s in ALL if s.inventory_func}


def download_funcs():
    """site id -> model_job download function name."""
    return {s.site: s.download_func for s in ALL if s.download_func}


def supported_links_message():
    """The 'supported links: ...' sentence intake errors carry."""
    labels = ", ".join(_BY_SITE[site].label for site in MESSAGE_ORDER)
    return (f"supported links: {labels} model pages, or a direct link to a "
            ".stl/.3mf file (a Dropbox share link works too)")


__all__ = [
    "ALL", "DIRECT", "DIRECT_FILE_EXTS", "MESSAGE_ORDER", "ModelSource",
    "SOURCES", "by_host", "by_site", "download_funcs", "hosts_map",
    "inventory_funcs", "link_source", "site_for_host", "supported_links_message",
]
