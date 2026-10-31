#!/usr/bin/env python3
"""Fetch, inspect, and (only with --approved) slice Printables, MakerWorld or Thingiverse jobs,
plus direct file links to a .stl/.3mf on any public host (#607).

Normal path, one command:
  python3 -m print_handy.core.model_job fetch URL
It verifies current hardware against Klipper + Moonraker + Spoolman, inventories every
listed source profile/file, downloads only the selected source, audits the 3MF/STL, and
creates JOB.json. It DOES NOT slice, upload, or start a print.

After the operator selects a numbered source and explicitly approves slicing:
  python3 -m print_handy.core.model_job fetch URL --select N --approved
The baked site G-code is never used on this printer. Geometry is resliced against
the live-verified nozzle, loaded Spoolman filament, and configured motion limits. Upload and
print stay separate unless --print-now is given, which means the operator clicked
"bed clear" for exactly this job. `python3 -m print_handy.core.model_job print-now FILE|JOB.json` starts an
already sliced job the same way.
"""
import argparse, base64, hashlib, html, http.client, ipaddress, json, logging, math, os, re, shutil, socket, subprocess, sys, time, urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, NamedTuple

from .. import firmware_update, printers, settings
from ..printers.base import MultiColorUnsupported
from . import _external, filament_color, gcode_3mf, launch_state, printer_state, spoolman_client
from .site_accounts import SiteLoginNeeded

logger = logging.getLogger(__name__)

# Bambu Lab start confirmation (#271): the printer can take well over a minute after `project_file`
# to unpack the package and report it, so one snapshot is not evidence. Poll this often, for the
# [bambu] start_confirm_s window (default below). Both hooks are module attributes so tests can
# drive the window with a fake clock instead of sleeping.
BAMBU_START_CONFIRM_S = 90
BAMBU_START_POLL_S = 10
_confirm_clock = time.monotonic
_confirm_sleep = time.sleep

PROFILE = settings.current()["paths"]["hermes_home"]
STATE = settings.state_dir()/"printer_hardware.json"
ARCHIVE = settings.current()["paths"]["archive"]
MOONRAKERCTL = settings.ROOT/"bin"/"moonrakerctl"


def _moonrakerctl_env(st):
    """Environment for bin/moonrakerctl aimed at the job's printer.

    moonrakerctl reads only MOONRAKER_URL / services.moonraker_url, the
    global (default printer's) URL; on a roster the start goes to the job's
    printer (st), so the upload must go there too.
    """
    env = dict(os.environ)
    url = st.get("moonraker_url") if isinstance(st, dict) else None
    if url:
        env["MOONRAKER_URL"] = str(url).rstrip("/")
    return env
TIMEZONE = settings.current()["discord"]["timezone"]
FILAMENT_SLOTS = settings.current()["printer"]["filament_slots"]
BED_MM = int(min(settings.current()["printer"]["bed_mm"]))
MAX_HEIGHT_MM = settings.current()["printer"]["max_print_height_mm"]

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def request_json(url, *, data=None, headers=None, timeout=25):
    h={"User-Agent":UA, "Accept":"application/json"}; h.update(headers or {})
    body=json.dumps(data).encode() if data is not None else None
    req=urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)


class _DropAuthOnHostChange(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry an Authorization header to another host.

    A site's signed-in download often redirects to signed object storage
    (MyMiniFactory to dl4.myminifactory.com, verified 2026-10-29): that host
    must not receive the operator's token, and it rejects a signed URL that
    also carries an Authorization header (HTTP 400 InvalidArgument).
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


def download(url, target, headers=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    h={"User-Agent":UA}; h.update(headers or {})
    req=urllib.request.Request(url, headers=h)
    from .intake_resources import save_download
    opener=urllib.request.build_opener(_DropAuthOnHostChange) if "Authorization" in h else None
    with (opener.open(req, timeout=90) if opener else urllib.request.urlopen(req, timeout=90)) as r:
        save_download(r, target)
    if target.stat().st_size == 0: raise RuntimeError("downloaded an empty file")


def maker_headers():
    # The token file, else MAKERWORLD_TOKEN (site_accounts.stored).
    from .site_accounts import stored
    path=Path.home()/".config/bambu/token"
    token, _=stored("makerworld")
    if token is None:
        raise SiteLoginNeeded("makerworld", "missing", f"MakerWorld links need an account token in {path}; "
                              "see docs/PROJECT-REFERENCE.md (MakerWorld account token)")
    if not token: raise SiteLoginNeeded("makerworld", "empty", "MakerWorld token empty")
    return {"Authorization":f"Bearer {token}", "Origin":"https://makerworld.com",
            "Referer":"https://makerworld.com/", "x-bbl-app-source":"makerworld"}


CJK=re.compile(r'[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]')
def english(*candidates):
    """First non-empty candidate without CJK text; else the first non-empty one.
    MakerWorld returns the author's original-language title plus an English
    `titleTranslated`. Discord output must stay English."""
    vals=[str(c).strip() for c in candidates if c and str(c).strip()]
    for v in vals:
        if not CJK.search(v): return v
    return vals[0] if vals else ""


def maker_id(url):
    m=re.search(r'/models/(\d+)',url)
    if not m: raise ValueError("MakerWorld URL needs /models/<number>")
    return m.group(1)


def maker_inventory(url):
    mid=maker_id(url)
    try:
        d=request_json(f"https://api.bambulab.com/v1/design-service/design/{mid}",headers=maker_headers())
    except urllib.error.HTTPError as exc:
        refused=_maker_refused(exc)
        if refused is not None: raise refused from None
        raise
    rows=[]
    for ix, i in enumerate(d.get("instances",[]),1):
        mi=i.get("extention",{}).get("modelInfo",{})
        c=mi.get("compatibility",{})
        plates=mi.get("plates",[])
        fs=i.get("instanceFilaments") or []
        from .makerworld_profiles import annotate as annotate_maker_profiles
        rows.append({"n":ix,"instance_id":i["id"],"title":english(i.get("titleTranslated"), i.get("title")) or "untitled",
                     "site_nozzle_mm":c.get("nozzleDiameter"),"site_printer":c.get("devProductName"),
                     "compatibility":c,"other_compatibility":mi.get("otherCompatibility") or [],
                     "plates":[{"name":p.get("name"),"objects":len(p.get("objects") or []),
                                "weight_g":p.get("weight"),"prediction_s":p.get("prediction")} for p in plates],
                     "materials":[f.get("type") for f in fs],
                     "filaments": [{"source_index": n, "material": f.get("type"), "color": f.get("color")}
                                   for n, f in enumerate(fs, 1)],
                     "source_parts": [{"plate": n, "objects": p.get("objects") or []}
                                      for n, p in enumerate(plates, 1)],
                     "is_default":i.get("isDefault",False)})
    from .makerworld_profiles import annotate as annotate_maker_profiles
    annotate_maker_profiles(rows)
    title=english(d.get("titleTranslated"), d.get("title"), d.get("name")) or f"makerworld-{mid}"
    return {"site":"makerworld","model_id":mid,"title":title,"slug":d.get("slug") or mid,
            "license":d.get("license"),"summary":html.unescape(re.sub('<[^>]+>',' ',english(d.get("summaryTranslated"), d.get("summary")) or "")).strip(),"sources":rows}


def _cached_source(site, key, value, model_id=None):
    """Newest earlier download of the same site file, so re-runs never hit the site again (rate limits, captcha)."""
    if value is None: return None
    best=None
    for jp in ARCHIVE.glob("*/JOB.json"):
        try:
            d=json.loads(jp.read_text())
        except (OSError, ValueError):
            continue
        if d.get("site")!=site or (model_id is not None and str(d.get("model_id"))!=str(model_id)):
            continue
        # A multi-file job records each file's own download in selected_sources.
        rows=[(d.get("selected_source") or {}, d.get("downloaded_file"))]+[
            (r, r.get("downloaded_file")) for r in d.get("selected_sources") or [] if isinstance(r, dict)]
        for sel, path in rows:
            f=Path(path or "")
            if (isinstance(sel, dict) and str(sel.get(key))==str(value)
                    and f.is_file() and f.stat().st_size>0):
                if best is None or f.stat().st_mtime>best.stat().st_mtime: best=f
    return best


def _cached_maker_source(instance_id):
    return _cached_source("makerworld", "instance_id", instance_id)


def _reuse_cached(cached, target, site):
    target.mkdir(parents=True, exist_ok=True)
    dest=target/cached.name
    if not (dest.exists() and os.path.samefile(dest, cached)):
        # A hard link, not a copy: every resend of the same model used to add a full copy to the
        # archive. The source is never edited in place (its SHA-256 binds every later step). An
        # existing dest is removed first, never written through, so a linked file is never truncated.
        dest.unlink(missing_ok=True)
        try:
            os.link(cached, dest)
        except OSError:  # another filesystem, or links unsupported
            shutil.copy2(cached, dest)
    print(f"SOURCE REUSED: {cached} (already downloaded; {site} not contacted)")
    return dest


def _maker_refused(exc):
    """SiteLoginNeeded for a MakerWorld 401/403 (the account token), else None.

    The raw HTTPError text ("HTTP Error 401: Unauthorized") names no site,
    so the #590 known-cause check could not recognise it and the operator
    got the generic bundle advice; this names the token and lets intake
    offer the renew flow. A 418 robot check is handled separately.
    """
    if getattr(exc, "code", None) not in (401, 403): return None
    return SiteLoginNeeded("makerworld", "refused",
        f"MakerWorld refused the request ({exc.code}), which usually means the MakerWorld "
        "account token is missing, invalid or expired. Sign in at makerworld.com, copy the "
        "token cookie into ~/.config/bambu/token (see docs/PROJECT-REFERENCE.md, MakerWorld "
        "account token), then send the link again.")


def maker_download(job, source, target):
    cached=_cached_maker_source(source.get("instance_id"))
    if cached is not None:
        return _reuse_cached(cached, target, "MakerWorld")
    try:
        r=request_json(f"https://api.bambulab.com/v1/design-service/instance/{source['instance_id']}/f3mf?type=download",headers=maker_headers())
    except urllib.error.HTTPError as exc:
        if exc.code == 418:
            # MakerWorld's Cloudflare front answers file downloads with 418 + a GeeTest captcha when it decides the
            # account/IP looks automated. Never try to solve or dodge it; say so plainly.
            raise RuntimeError("MakerWorld blocked the download with a robot check (captcha). Open the model on "
                               "makerworld.com while logged in and pass the check there, or wait a while, then send the link again") from None
        refused=_maker_refused(exc)
        if refused is not None: raise refused from None
        raise
    name=Path(r.get("name") or "model.3mf").name
    target=target/name
    download(r["url"],target)
    return target

PRINTABLES="https://api.printables.com/graphql/"
def printables_id(url):
    m=re.search(r'/model/(\d+)',url)
    if not m: raise ValueError("Printables URL needs /model/<number>")
    return m.group(1)
def gql(query, variables):
    return request_json(PRINTABLES,data={"query":query,"variables":variables},headers={"Content-Type":"application/json","Origin":"https://www.printables.com","Referer":"https://www.printables.com/"})
def printables_inventory(url):
    mid=printables_id(url)
    q='''query($id: ID!) { print(id:$id) { id name slug license{name} stls{id name fileSize} gcodes{id name fileSize} otherFiles{id name fileSize} } }'''
    d=gql(q,{"id":mid}).get("data",{}).get("print")
    if not d: raise RuntimeError("Printables model not found")
    files=[]
    for kind in ("stls","gcodes","otherFiles"):
        for f in d.get(kind,[]): files.append({"id":f["id"],"name":f["name"],"bytes":f["fileSize"],"kind":kind})
    job={"site":"printables","model_id":mid,"title":d["name"],"slug":d.get("slug") or mid,"license":(d.get("license")or{}).get("name"),"sources":files}
    # Stage 0 of #462 (live-audited 2026-10-27, model 3111): some listings expose no
    # geometry at all through this API -- stls and otherFiles come back empty and only
    # pre-sliced G-code is listed, and PrintType has no other file field to reach it
    # through (files/modelFiles/packages/downloads and similar are all rejected by
    # the schema). Print Handy never uses baked site G-code, so name that exact reason
    # instead of letting intake report a generic empty inventory.
    if not any(Path(f["name"]).suffix.lower() in {".stl", ".3mf"} for f in files):
        detail=[]
        if d.get("gcodes"): detail.append(f"only pre-sliced G-code is listed ({len(d['gcodes'])} file(s))")
        if d.get("otherFiles"): detail.append("otherFiles lists only non-geometry files: "
                                              + ", ".join(str(f.get("name")) for f in d["otherFiles"]))
        if not detail: detail.append("no files are listed at all")
        job["no_geometry_reason"]=(
            f"Printables listing for model {mid} exposes no STL/3MF via the Printables API "
            f"(stls is empty; {'; '.join(detail)}), so there is no selectable geometry to reslice")
    return job
def printables_download(job, source, target):
    cached=_cached_source("printables", "id", source.get("id"), job.get("model_id"))
    if cached is not None and cached.name==Path(source.get("name") or "").name:
        return _reuse_cached(cached, target, "Printables")
    q='''mutation($id:ID!,$printId:ID!,$source:DownloadSourceEnum!,$fileType:DownloadFileTypeEnum!){getDownloadLink(id:$id,printId:$printId,source:$source,fileType:$fileType){ok output{link} errors{messages}}}'''
    typ="gcode" if source["kind"] == "gcodes" else "stl"
    d=gql(q,{"id":str(source["id"]),"printId":str(job["model_id"]),"source":"model_detail","fileType":typ})["data"]["getDownloadLink"]
    if not d.get("ok"): raise RuntimeError(str(d.get("errors")))
    target=target/Path(source["name"]).name; download(d["output"]["link"],target); return target

THINGIVERSE="https://api.thingiverse.com"
def thingiverse_headers():
    # The token file, else THINGIVERSE_TOKEN (site_accounts.stored).
    from .site_accounts import stored
    path=Path.home()/".config/thingiverse/token"
    token, _=stored("thingiverse")
    if token is None:
        raise SiteLoginNeeded("thingiverse", "missing", f"Thingiverse links need a free Thingiverse App Token in {path}; "
                              "create an app/token at https://www.thingiverse.com/developers "
                              "(under your Thingiverse account) and store the App Token as the only "
                              "content of that file; see docs/PROJECT-REFERENCE.md (Thingiverse App Token)")
    if not token: raise SiteLoginNeeded("thingiverse", "empty", "Thingiverse token empty")
    return {"Authorization":f"Bearer {token}"}
def _thingiverse_blocked(exc):
    """Plain RuntimeError for a Thingiverse 401/403/challenge, else None. Never bypass a challenge."""
    if getattr(exc, "code", None) not in (401, 403): return None
    headers=getattr(exc, "headers", None) or {}
    try: challenged="challenge" in str(headers.get("Cf-Mitigated", "")).lower()
    except Exception: challenged=False
    cause=("Thingiverse answered with a Cloudflare robot check (challenge)" if challenged
           else "Thingiverse refused the request (401/403), which usually means the App Token is missing, invalid or expired, or the site is challenging this connection")
    # A plain 401 is the token itself: offer the connect flow (renew).
    kind=(lambda message: SiteLoginNeeded("thingiverse", "refused", message)) \
        if getattr(exc, "code", None) == 401 and not challenged else RuntimeError
    return kind(f"{cause}. Open thingiverse.com in a browser and pass any check there, "
                        "check the token file (see docs/PROJECT-REFERENCE.md, Thingiverse App Token), "
                        "then send the link again. Print Handy does not try to bypass robot checks.")
def _thingiverse_json(url):
    try:
        return request_json(url, headers=thingiverse_headers())
    except urllib.error.HTTPError as exc:
        blocked=_thingiverse_blocked(exc)
        if blocked is not None: raise blocked from None
        raise
def thingiverse_id(url):
    m=re.search(r'thing:(\d+)', url)
    if not m: raise ValueError("Thingiverse URL needs /thing:<number>")
    return m.group(1)
def thingiverse_inventory(url):
    # Verified against the official API shape documented by open-source SDKs
    # (e.g. the Go thingiverse client): GET /things/{id} -> {id, name, license (string),
    # public_url, ...}; GET /things/{id}/files -> [{id, name, size, download_url, ...}].
    # The per-file download_url is the download route. Verified end to end live on
    # 2026-10-29 with an operator App Token (thing 763622: lookup, file list and a real
    # STL download); the token also passes the Cloudflare check that answers
    # anonymous requests.
    mid=thingiverse_id(url)
    d=_thingiverse_json(f"{THINGIVERSE}/things/{mid}")
    if not isinstance(d, dict) or not d: raise RuntimeError("Thingiverse thing not found")
    listed=_thingiverse_json(f"{THINGIVERSE}/things/{mid}/files")
    rows=[]
    for f in listed or []:
        name=str(f.get("name") or "")
        if Path(name).suffix.lower() not in {".stl", ".3mf"}: continue
        rows.append({"id":f.get("id"),"name":name,"bytes":f.get("size"),"kind":"files",
                     "download_url":f.get("download_url")})
    title=str(d.get("name") or "").strip() or f"thingiverse-{mid}"
    slug=re.sub(r'[^a-z0-9]+','-',title.lower()).strip('-') or mid
    return {"site":"thingiverse","model_id":mid,"title":title,"slug":slug,
            "license":d.get("license"),"sources":rows}
def thingiverse_download(job, source, target):
    cached=_cached_source("thingiverse", "id", source.get("id"), job.get("model_id"))
    if cached is not None and cached.name==Path(source.get("name") or "").name:
        return _reuse_cached(cached, target, "Thingiverse")
    url=source.get("download_url")
    if not url:
        for f in _thingiverse_json(f"{THINGIVERSE}/things/{job['model_id']}/files") or []:
            if str(f.get("id"))==str(source.get("id")): url=f.get("download_url"); break
    if not url: raise RuntimeError("Thingiverse gave no download_url for this file, so it cannot be downloaded through the API")
    dest=target/Path(source["name"]).name
    try:
        download(url, dest, headers=thingiverse_headers())
    except urllib.error.HTTPError as exc:
        blocked=_thingiverse_blocked(exc)
        if blocked is not None: raise blocked from None
        raise
    return dest

MYMINIFACTORY="https://www.myminifactory.com/api/v2"
# MyMiniFactory downloads (#464): per MyMiniFactory's own API guidelines
# (https://www.myminifactory.com/pages/api-guidelines), browsing needs no user
# login, but downloading an object inside an app requires the downloader to be
# signed in to their MyMiniFactory account (OAuth). The operator signs in once
# through MyMiniFactory's own OAuth page (site_accounts, "myminifactory") and
# the access token is the saved login. Verified live end to end 2026-10-29:
# with that token the API returns each file's download_url (an API key alone
# returns none); the download URL answers the signed-in request with a 302 to
# signed storage on dl4.myminifactory.com, and the file arrives intact. The
# token must not follow that redirect (see _DropAuthOnHostChange). Cloudflare
# fronts www.myminifactory.com and has challenged other clients there; a
# challenge is a plain stop with the browser workaround — Print Handy never
# tries to get past it.
MYMINIFACTORY_CHALLENGE_GAP=(
    "MyMiniFactory's download page answered with a Cloudflare robot check, so nothing "
    "was downloaded. Print Handy never tries to get past a robot check. This depends "
    "on the network Print Handy runs from. Workaround: open the model page in a browser, "
    "download the .stl/.3mf file there, then upload or drop the file here instead.")
MYMINIFACTORY_DOWNLOAD_GAP=(
    "MyMiniFactory did not give a download link for this file, so nothing was "
    "downloaded. Workaround: open the model page in a browser, download the .stl/.3mf "
    "file there, then upload or drop the file here instead.")
def myminifactory_api_key():
    path=Path.home()/".config/myminifactory/token"
    try:
        key=path.read_text().strip()
    except FileNotFoundError:
        raise RuntimeError(f"MyMiniFactory links need an API key in {path}; create an API key "
                           "in your MyMiniFactory account and store it as the only content of "
                           "that file; see docs/PROJECT-REFERENCE.md (MyMiniFactory API key)") from None
    if not key: raise RuntimeError("MyMiniFactory API key empty")
    return key
def _mmf_blocked(exc):
    """Plain RuntimeError for a MyMiniFactory 401/403, else None. Never bypass a challenge."""
    if getattr(exc, "code", None) not in (401, 403): return None
    return RuntimeError("MyMiniFactory refused the request (401/403), which usually means the API key "
                        "is missing, invalid or expired, or the site is challenging this connection. "
                        "Check the API key file (see docs/PROJECT-REFERENCE.md, MyMiniFactory API key), "
                        "then send the link again. Print Handy does not try to bypass robot checks.")
def _mmf_json(path):
    """GET an API v2 path with the API key, or with the OAuth sign-in when there is no key file."""
    sep="&" if "?" in path else "?"
    try:
        key=myminifactory_api_key()
    except RuntimeError as missing:
        from .site_accounts import read_token
        try:
            token=read_token("myminifactory")
        except SiteLoginNeeded as no_sign_in:
            # Neither credential: the sign-in covers listing AND download,
            # so the connect flow offers that; the key-file remedy stays named.
            raise SiteLoginNeeded("myminifactory", no_sign_in.reason,
                                  f"{missing} Or connect the MyMiniFactory sign-in, which also "
                                  "downloads: see docs/PROJECT-REFERENCE.md (MyMiniFactory sign-in).") from None
        url, headers=f"{MYMINIFACTORY}{path}", {"Authorization":f"Bearer {token}"}
    else:
        url, headers=f"{MYMINIFACTORY}{path}{sep}key={key}", None
    try:
        return request_json(url, headers=headers)
    except urllib.error.HTTPError as exc:
        if headers and exc.code == 401:
            # The OAuth sign-in itself was refused (revoked or expired):
            # offer the renew flow, not the API-key remedy.
            from .site_accounts import refused
            raise refused("myminifactory") from None
        blocked=_mmf_blocked(exc)
        if blocked is not None: raise blocked from None
        raise
def myminifactory_id(url):
    m=re.search(r'/object/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)', url)
    if not m: raise ValueError("MyMiniFactory URL needs /object/...-<number> "
                               "(an object page URL ending in its number)")
    return m.group(1)
def myminifactory_inventory(url):
    # The shape follows MyMiniFactory API v2's documented object response
    # (GET /objects/{id} -> {id, name, license, files}), parsed defensively: the
    # file list may be a plain list or {"items": [...]}, a file's name may sit
    # under "filename" or "name", its size under "size", and the license may be
    # a string or {"name": ...}. No live request has verified this shape: no
    # API key was available when this was built (2026-10-27), which the #462
    # Stage 2 PR body names explicitly.
    mid=myminifactory_id(url)
    d=_mmf_json(f"/objects/{mid}")
    if not isinstance(d, dict) or not d: raise RuntimeError("MyMiniFactory object not found")
    listed=d.get("files")
    if isinstance(listed, dict): listed=listed.get("items")
    rows=[]
    for f in listed or []:
        if not isinstance(f, dict): continue
        name=str(f.get("filename") or f.get("name") or "")
        if Path(name).suffix.lower() not in {".stl", ".3mf"}: continue
        rows.append({"id":f.get("id"),"name":name,"bytes":f.get("size"),"kind":"files"})
    title=str(d.get("name") or "").strip() or f"myminifactory-{mid}"
    slug=re.sub(r'[^a-z0-9]+','-',title.lower()).strip('-') or mid
    license_value=d.get("license")
    if isinstance(license_value, dict): license_value=license_value.get("name")
    job={"site":"myminifactory","model_id":mid,"title":title,"slug":slug,
         "license":license_value,"sources":rows}
    if not rows:
        job["no_geometry_reason"]=(
            f"MyMiniFactory listing for object {mid} exposes no STL/3MF in its file "
            "list, so there is no selectable geometry to reslice")
    return job
def myminifactory_download(job, source, target):
    cached=_cached_source("myminifactory", "id", source.get("id"), job.get("model_id"))
    if cached is not None and cached.name == Path(source.get("name") or "").name:
        return _reuse_cached(cached, target, "MyMiniFactory")
    from .site_accounts import read_token, refused
    token=read_token("myminifactory")
    auth={"Authorization":f"Bearer {token}"}
    # GET /files/{id} is "information of a file including the download link";
    # download_url is filled in only for an OAuth-signed-in user.
    status, d=_web_api("GET", f"{MYMINIFACTORY}/files/{source.get('id')}", headers=auth)
    if status == 401: raise refused("myminifactory")
    if status == 403: raise RuntimeError(MYMINIFACTORY_CHALLENGE_GAP)
    url=d.get("download_url") if isinstance(d, dict) else None
    if not url: raise RuntimeError(MYMINIFACTORY_DOWNLOAD_GAP)
    try:
        return _fetch_chosen(url, target, source.get("name"), "MyMiniFactory", exact=True, headers=auth)
    except urllib.error.HTTPError as exc:
        if exc.code == 403 and (exc.headers or {}).get("cf-mitigated"):
            raise RuntimeError(MYMINIFACTORY_CHALLENGE_GAP) from None
        if exc.code in (401, 403): raise refused("myminifactory", f" ({exc.code})") from None
        raise


CULTS3D = "https://cults3d.com/graphql"

# Cults3D is metadata-only through its API, permanently: Cults' own API page
# (https://cults3d.com/en/api) states the API "will not give you access to the
# 3D files… The files remain hosted on Cults" and are downloaded only through
# the signed-in website. So, like MyMiniFactory, the provider below is
# inventory (the listing's title) plus an honest refusal, and the refusal IS
# the download behaviour: no credential an operator can supply changes it,
# and Print Handy never scrapes the site or tries to get around its checks.
CULTS_DOWNLOAD_GAP = (
    "Print Handy cannot download files from Cults3D. Cults' own API page states "
    "the API gives access to a listing's details (photos, titles, descriptions, "
    "tags) but not its 3D files — the files remain hosted on Cults and are "
    "downloaded through the signed-in website. Nothing was downloaded. "
    "Workaround: open the model page in a browser, download the .stl/.3mf file "
    "there, then send that file to Print Handy instead (upload it or drop it in "
    "the folder Print Handy watches).")


def cults_credentials():
    """Return the Authorization header value for Cults' GraphQL API.

    Cults API keys are per-account and authenticate as HTTP Basic
    `nickname:api_key` (https://cults3d.com/en/api). Following the token-file
    pattern of the other model sites, the pair is the only content of
    ~/.config/cults3d/token, formatted `nickname:key` — the nickname must
    match the Cults profile exactly, capitals included. The key is never
    logged, and no error message includes it.
    """
    from .site_accounts import stored
    path = Path.home()/".config"/"cults3d"/"token"
    raw, _ = stored("cults3d")  # the token file, else CULTS3D_TOKEN
    if raw is None:
        raise SiteLoginNeeded("cults3d", "missing",
            f"Cults3D needs an API key first. Create one at https://cults3d.com/en/api/keys "
            f"and save it as the only content of {path} in the form nickname:key (your Cults "
            "nickname, a colon, then the API key), readable only by the account that runs "
            "Print Handy. See docs/PROJECT-REFERENCE.md (Cults3D API key).")
    nickname, sep, key = raw.partition(":")
    if not sep or not nickname.strip() or not key.strip():
        raise SiteLoginNeeded("cults3d", "empty",
            f"The Cults3D token file at {path} must contain exactly nickname:key — your "
            "Cults nickname, a colon, then the API key from "
            "https://cults3d.com/en/api/keys — as its only content.")
    return "Basic " + base64.b64encode(
        f"{nickname.strip()}:{key.strip()}".encode()).decode()


def _cults_blocked(exc):
    code = getattr(exc, "code", None)
    if code == 401:
        return SiteLoginNeeded("cults3d", "refused",
            "Cults3D refused the request (401), which usually means the nickname or API "
            "key in ~/.config/cults3d/token is wrong — the nickname must match your Cults "
            "profile exactly, capitals included. Check the file, then send the link again.")
    if code == 403:
        return RuntimeError(
            "Cults3D refused the request (403), which can mean the site is challenging "
            "automated access. Print Handy never tries to solve or bypass a site's "
            "checks. Open cults3d.com in a browser, check the API key at "
            "https://cults3d.com/en/api/keys, then send the link again.")
    return exc


def _cults_graphql(query, variables):
    try:
        return request_json(CULTS3D, data={"query": query, "variables": variables},
                            headers={"Content-Type": "application/json",
                                     "Authorization": cults_credentials()})
    except urllib.error.HTTPError as exc:
        raise _cults_blocked(exc) from exc


def cults_id(url):
    """The creation slug from a Cults model URL (/3d-model/<category>/<slug>)."""
    path = urllib.parse.urlparse(str(url)).path
    m = re.search(r"/3d-model/(?:[^/]+/)?([^/]+)/?$", path)
    if not m: raise ValueError(f"Cults3D URL needs /3d-model/.../<slug>: {url}")
    return m.group(1)


def cults_inventory(url):
    """Listing metadata for a Cults URL, via Cults' documented GraphQL API.

    Endpoint https://cults3d.com/graphql, HTTP Basic nickname:api_key, and
    the creation is looked up by its slug: `creation(slug:)`. Verified live
    2026-10-29 with an operator key: a real slug answers {id, name, url,
    license{name}} and an unknown slug answers creation: null. (The first
    build guessed a Relay `node(id:)` lookup with a padded base64 id, which
    never matched: Cults' ids are unpadded.) The returned id is still checked
    against the requested slug. The API exposes no file list at all, so
    `sources` is always empty and the job carries the download gap plus a
    no-geometry reason that names the listing it did find.
    """
    slug = cults_id(url)
    resp = _cults_graphql(
        "query($slug: String!) { creation(slug: $slug) { id name url license { name } } }",
        {"slug": slug})
    data = resp.get("data") if isinstance(resp, dict) else None
    node = (data or {}).get("creation") or (data or {}).get("node")
    errors = resp.get("errors") if isinstance(resp, dict) else None
    if not isinstance(node, dict) or (errors and not node):
        raise RuntimeError(
            f"Cults3D creation not found for {url} — the link may be wrong, the "
            "creation may have been removed, or Cults may have changed its API.")
    returned = str(node.get("id") or "")
    try:
        decoded = base64.b64decode(returned + "=" * (-len(returned) % 4)).decode()
    except Exception:
        decoded = ""
    if decoded != f"Creation/{slug}":
        raise RuntimeError(
            f"Cults3D creation not found for {url} — the API answered with a "
            "different creation, so the link was not followed.")
    title = str(node.get("name") or "").strip() or f"cults3d-{slug}"
    reason = (
        f"Cults3D listing \"{title}\" was found through the Cults API, but the API "
        "does not expose a creation's files — it returns listing details only — and "
        "Cults downloads happen only through the signed-in website, so there is no "
        "selectable geometry to reslice. Workaround: open the model page in a "
        "browser, download the .stl/.3mf file there, then upload or drop the file "
        "here instead.")
    license_value = (node.get("license") or {}).get("name") if isinstance(node.get("license"), dict) else None
    return {"site": "cults3d", "model_id": slug, "title": title,
            "slug": slug, "license": license_value, "sources": [],
            "download_gap": CULTS_DOWNLOAD_GAP, "no_geometry_reason": reason}


def cults_download(job, source, target):
    """Refuse plainly: Cults' API exposes no files (see CULTS_DOWNLOAD_GAP)."""
    raise RuntimeError(CULTS_DOWNLOAD_GAP)


CREALITYCLOUD = "https://www.crealitycloud.com"

# Creality Cloud (#604, reopening the #464 classification): the web app's own
# JSON API. The app proxies /api/* to its configured API base
# (admin.crealitycloud.com) and answers with {code, msg, result} envelopes;
# code 0 is success and code 4 is the token refusal (ERR_TOKEN in the web
# client's own error table). Model detail and the source-file inventory are
# served anonymously; downloads go through memberDownload, which requires
# the account login token the web client keeps in its model_token cookie.
# Following the token-file pattern of the other model sites, the operator
# copies that cookie value into ~/.config/crealitycloud/token. No password
# is ever stored, and no site check is bypassed. Verified live 2026-10-29
# against the acceptance model (bag-clip-30mm): detail and file list
# answered anonymously with code 0, and memberDownload answered
# {"code": 4, "msg": "Invalid Token"} without a token — so the success
# shape of its result was NOT verifiable from here and is parsed
# defensively in _crealitycloud_download_url; an answer with no findable
# file URL refuses with a named gap instead of guessing.
CREALITYCLOUD_ANSWER_GAP = (
    "Creality Cloud answered the download request, but Print Handy could not "
    "find a file URL in the answer, so nothing was downloaded (the site may "
    "have changed its answer). Workaround: open "
    "the model page in a browser, download the .stl/.3mf file there, then "
    "upload or drop the file here instead.")


def crealitycloud_headers(token=None):
    """The __CXY_* headers the Creality Cloud web client itself sends.

    __CXY_TIMEZONE_ is not optional decoration: without it the API
    refuses every call with code 1 (Invalid Parameter), verified live
    2026-10-29. Its value is the local UTC offset in seconds, the web
    client's own formula.

    ``token`` is the saved login, ``model_user_id:model_token``: the web
    client sends the user id (its model_user_id cookie) as __CXY_UID_
    beside the token, and the API answers "Invalid Token" to a valid
    token sent without it (verified live 2026-10-29).
    """
    import datetime
    uid, _, token = (token or "").rpartition(":")
    offset = datetime.datetime.now().astimezone().utcoffset()
    return {"Content-Type": "application/json",
            "__CXY_APP_ID_": "cxy-gen2", "__CXY_APP_VER_": "7.3.28",
            "__CXY_PLATFORM_": "2", "__CXY_BRAND_": "creality",
            "__CXY_APP_CH_": "not set", "__CXY_OS_VER_": "not set",
            "__CXY_DUID_": "not set", "__CXY_OS_LANG_": "0",
            "__CXY_TOKEN_": token, "__CXY_UID_": uid,
            "__CXY_TIMEZONE_": str(int(offset.total_seconds())) if offset else "0",
            "__CXY_REQUESTID_": os.urandom(16).hex()}


def crealitycloud_token():
    """The saved login, ``model_user_id:model_token``.

    From the token file, else CREALITYCLOUD_TOKEN (site_accounts.read_token).
    A value without the user id cannot work — the API refuses the token
    alone — so it is named here, before any request.
    """
    from .site_accounts import read_token
    token = read_token("crealitycloud")
    uid, sep, rest = token.partition(":")
    if not sep or not uid.strip().isdigit() or not rest.strip():
        raise SiteLoginNeeded("crealitycloud", "missing", (
            "The saved Creality Cloud login needs both cookie values, written as "
            "model_user_id:model_token (the number from the model_user_id cookie, a "
            "colon, then the model_token cookie value). Only one value is saved now. "
            "See docs/PROJECT-REFERENCE.md (Creality Cloud login token)."))
    return f"{uid.strip()}:{rest.strip()}"


def _crealitycloud_token_refused():
    return SiteLoginNeeded("crealitycloud", "refused", 
        "Creality Cloud refused the login token (the site answered \"Invalid "
        "Token\"), which usually means the token in "
        "~/.config/crealitycloud/token is missing, invalid or expired. Sign "
        "in at https://www.crealitycloud.com, copy the model_user_id and "
        "model_token cookie values into that file as model_user_id:model_token "
        "(see docs/PROJECT-REFERENCE.md, Creality Cloud login token), then "
        "send the link again.")


def _crealitycloud_blocked(exc):
    """Plain RuntimeError for a Creality Cloud 401/403, else None."""
    if getattr(exc, "code", None) not in (401, 403): return None
    return RuntimeError(
        "Creality Cloud refused the request (401/403), which usually means "
        "the login token is missing, invalid or expired, or the site is "
        "challenging this connection. Check the token file (see "
        "docs/PROJECT-REFERENCE.md, Creality Cloud login token), then send "
        "the link again. Print Handy does not try to bypass site checks.")


def _crealitycloud_request(path, body, token=None):
    """One POST to the Creality Cloud web API; returns the parsed envelope.

    A local transport rather than the shared request_json, for one
    live-verified reason: this API's edge truncates uncompressed chunked
    reads often enough to fail most calls (IncompleteRead), while the
    same calls with gzip encoding succeed almost every time (observed
    2026-10-29). Retries live in _crealitycloud_result.
    """
    import gzip
    headers = crealitycloud_headers(token)
    headers["Accept-Encoding"] = "gzip"
    headers["User-Agent"] = UA
    headers["Accept"] = "application/json"
    req = urllib.request.Request(f"{CREALITYCLOUD}{path}",
                                 data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _crealitycloud_result(path, body, token=None):
    """One Creality Cloud API call; returns the envelope's result.

    HTTP stays 200 for API refusals — the envelope code decides. Known
    codes are named with their remedy; an unknown code is reported by
    number only, never by copying the site's message text into chat.
    Transport-level dropped reads are retried (see _crealitycloud_request):
    every call made here is an idempotent read or a download request, and
    envelope refusals are never retried.
    """
    resp = None
    for attempt in range(4):
        try:
            resp = _crealitycloud_request(path, body, token)
            break
        except urllib.error.HTTPError as exc:
            blocked = _crealitycloud_blocked(exc)
            if blocked is not None: raise blocked from None
            raise
        except (http.client.HTTPException, ConnectionError, TimeoutError,
                urllib.error.URLError, ValueError):
            if attempt == 3: raise
            time.sleep(0.5 * (attempt + 1))
    if not isinstance(resp, dict) or "code" not in resp:
        raise RuntimeError("Creality Cloud answered with an unexpected "
                           "response, so the request was not followed")
    code = resp.get("code")
    if code == 0: return resp.get("result")
    if code == 4: raise _crealitycloud_token_refused()
    if code in (512, 517):
        raise RuntimeError("Creality Cloud says this model does not exist — "
                           "it may have been removed, or the link is wrong")
    raise RuntimeError(f"Creality Cloud refused the request (code {code})")


def crealitycloud_slug(url):
    """The model slug from a Creality Cloud URL (/model-detail/<slug>)."""
    path = urllib.parse.urlparse(str(url)).path
    m = re.search(r"/model-detail/([^/]+)/?$", path)
    if not m: raise ValueError(f"Creality Cloud URL needs /model-detail/<slug>: {url}")
    return m.group(1)


def crealitycloud_profile_id(url):
    """The ?profileId= print-profile id from a Creality Cloud URL, or None."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(str(url)).query)
    values = query.get("profileId") or []
    return values[0] if values else None


def _crealitycloud_profile_file(group_id, profile_id):
    """Source file name inside one 3MF print profile, or None (best effort).

    A profile is a Creality Print 3MF holding one source file; the detail
    the API pre-parses from it names that file. A failed lookup returns
    None — the profile hint never blocks the inventory.
    """
    try:
        listing = _crealitycloud_result(
            "/api/cxy/v3/model/3mfList",
            {"modelGroupId": group_id, "page": 1, "pageSize": 100})
    except Exception:
        return None
    for profile in (listing or {}).get("list") or []:
        if not isinstance(profile, dict): continue
        if str(profile.get("id")) != str(profile_id): continue
        infos = ((profile.get("preParse3mfResult") or {}).get("allObjectInfos")) or []
        for info in infos:
            name = str((info or {}).get("object_name") or "").strip()
            if name: return name
    return None


def crealitycloud_inventory(url):
    slug = crealitycloud_slug(url)
    detail = _crealitycloud_result("/api/cxy/v3/model/modelGroupDetail", {"id": slug})
    group = (detail or {}).get("groupItem") if isinstance(detail, dict) else None
    if not isinstance(group, dict) or not group.get("id"):
        raise RuntimeError(f"Creality Cloud model not found for {url} — the link "
                           "may be wrong, or the model may have been removed")
    group_id = str(group["id"])
    listing = _crealitycloud_result("/api/cxy/v3/model/fileListPage",
                                    {"modelId": group_id, "page": 1, "pageSize": 100})
    rows = []
    for f in (listing or {}).get("list") or []:
        if not isinstance(f, dict): continue
        fmt = str(f.get("fileFormat") or "").lower()
        if fmt not in {".stl", ".3mf"}: continue
        name = str(f.get("fileName") or "").strip()
        if not name: continue
        if not name.lower().endswith(fmt): name += fmt
        rows.append({"id": f.get("id"), "name": name, "bytes": f.get("fileSize"),
                     "kind": "files", "file_key": f.get("fileKey")})
    title = str(group.get("groupName") or "").strip() or f"crealitycloud-{slug}"
    job = {"site": "crealitycloud", "model_id": group_id, "title": title,
           "slug": str(group.get("urlAlias") or slug),
           "license": group.get("license"), "sources": rows}
    if group.get("isPay"): job["paid"] = True
    # The link's profileId names one print profile; the source file inside
    # it is the file the link points at, so offer that file first.
    profile_id = crealitycloud_profile_id(url)
    if profile_id and len(rows) > 1:
        pointed = _crealitycloud_profile_file(group_id, profile_id)
        for row in rows:
            if pointed and row["name"].casefold() == pointed.casefold():
                row["suggested_by_link"] = True
                rows.remove(row)
                rows.insert(0, row)
                break
    if not rows:
        job["no_geometry_reason"] = (
            f"Creality Cloud listing \"{title}\" exposes no STL/3MF source "
            "files in its file list, so there is no selectable geometry to "
            "reslice — Print Handy never uses a site's pre-sliced G-code or "
            "its printer-specific print profiles. If the model page offers "
            "source files the API does not list, download the .stl/.3mf "
            "file there in a browser, then upload or drop the file here "
            "instead.")
    return job


def _crealitycloud_download_url(result, file_id=None):
    """The file URL in a memberDownload answer, or None.

    Verified live 2026-10-29: the answer is {downloadUrls: [{fileName,
    url, fileId, expire}]}, and the entry for the requested ``file_id``
    is taken. Other shapes this API family uses are still accepted: a
    bare URL string, a dict carrying one under a URL key, or a list.
    """
    if isinstance(result, dict) and isinstance(result.get("downloadUrls"), list):
        rows = [r for r in result["downloadUrls"] if isinstance(r, dict) and r.get("url")]
        match = next((r for r in rows if file_id is not None and str(r.get("fileId")) == str(file_id)), None)
        if match is None and len(rows) == 1: match = rows[0]
        if match is not None: return match["url"]
    if isinstance(result, str): return result or None
    if isinstance(result, list):
        for item in result:
            found = _crealitycloud_download_url(item)
            if found: return found
        return None
    if isinstance(result, dict):
        for key in ("url", "downloadUrl", "downloadZip", "zipUrl", "fileUrl", "link"):
            value = result.get(key)
            if isinstance(value, str) and value: return value
        for key in ("list", "files", "urls"):
            found = _crealitycloud_download_url(result.get(key))
            if found: return found
    return None


def crealitycloud_download(job, source, target):
    cached = _cached_source("crealitycloud", "id", source.get("id"), job.get("model_id"))
    if cached is not None and cached.name == Path(source.get("name") or "").name:
        return _reuse_cached(cached, target, "Creality Cloud")
    token = crealitycloud_token()
    result = _crealitycloud_result(
        "/api/cxy/v3/model/memberDownload",
        {"modelId": job["model_id"], "trailType": 0,
         "fileIds": [source.get("id")]}, token=token)
    url = _crealitycloud_download_url(result, source.get("id"))
    if not url: raise RuntimeError(CREALITYCLOUD_ANSWER_GAP)
    # memberDownload names one file; a multi-file answer arrives as one zip
    # and keeps only the chosen file (_fetch_chosen, which also knows a 3MF
    # is itself a zip and must not be unpacked).
    return _fetch_chosen(url, target, source.get("name"), "Creality Cloud", exact=True)

# ---------------------------------------------------------------------------
# Login-token web-API sites (#464): Thangs and Nexprint (MakerOnline's
# inventory shares the transport but is inventory-only; see below).
#
# Like Creality Cloud above, each of these uses the JSON API its own web app
# calls. Model detail and the file list answer anonymously; the download
# needs the operator's login token (see site_accounts), which the operator
# copies from their signed-in browser into ~/.config/<site>/token. No
# password is stored, no captcha is solved, and no site check is bypassed:
# a robot check or captcha answer is a plain stop. Verified live 2026-10-29
# end to end for Thangs and Nexprint with operator tokens (inventory, the
# tokenless refusal, and a real download); answers are still parsed
# defensively, refusing with a named gap instead of guessing.
# ---------------------------------------------------------------------------

def _web_api(method, url, *, body=None, headers=None, timeout=25):
    """One JSON request; returns (HTTP status, parsed body or text). Never raises on 4xx/5xx.

    The sites below answer refusals with JSON bodies on 4xx statuses (and
    sometimes on 200), so the caller decides from both. gzip is requested
    (smaller and, on Creality's edge, more reliable); dropped reads are
    retried — every call here is an idempotent read or a download-link request.
    """
    import gzip
    h = {"User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "gzip"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status, raw, enc = r.status, r.read(), r.headers.get("Content-Encoding")
        except urllib.error.HTTPError as exc:
            status, enc = exc.code, (exc.headers.get("Content-Encoding") if exc.headers else None)
            raw = exc.read() or b""
        except (http.client.HTTPException, ConnectionError, TimeoutError, urllib.error.URLError):
            if attempt == 2: raise
            time.sleep(0.5 * (attempt + 1))
            continue
        break
    if enc == "gzip" and raw:
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def _answer_url(result):
    """The file URL in a download-link answer (string, dict or list), or None."""
    if isinstance(result, str): return result if result.startswith("http") else None
    if isinstance(result, list):
        for item in result:
            found = _answer_url(item)
            if found: return found
        return None
    if isinstance(result, dict):
        for key in ("signedUrl", "resultUrl", "url", "downloadUrl", "file_url", "fileUrl",
                    "downloadZip", "zipUrl", "link"):
            value = result.get(key)
            if isinstance(value, str) and value.startswith("http"): return value
        for key in ("fileInfoList", "list", "files", "urls", "data"):
            found = _answer_url(result.get(key))
            if found: return found
    return None


def _is_3mf_package(archive):
    names = set(archive.namelist())
    return "[Content_Types].xml" in names or "3D/3dmodel.model" in names


def _fetch_chosen(url, target, name, label, *, exact, headers=None):
    """Download ``url`` into ``target`` and keep only the chosen file ``name``.

    A zip answer (a whole-model download) is unpacked to the entry named
    ``name``, or to its only geometry file. A 3MF is itself a zip, so a zip
    that IS a 3MF package is the file, not an archive of files. A plain file
    answer is the chosen file when ``exact`` (the request named one file);
    otherwise its name must match, or nothing is kept.
    """
    name = Path(name or "model.stl").name
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    dest = target/name
    staged = target/(name + ".download")
    download(url, staged, headers)
    with staged.open("rb") as head:
        start = head.read(512).lstrip().lower()
    if start.startswith((b"<!doctype html", b"<html")):
        # A sign-in or robot-check page saved as if it were the model.
        staged.unlink(missing_ok=True)
        raise RuntimeError(f"{label} sent a web page instead of the file (usually a sign-in or "
                           "robot-check page), so nothing was kept. Download the file from the "
                           "model page in a browser and upload or drop it here instead.")
    archive = None
    if zipfile.is_zipfile(staged):
        archive = zipfile.ZipFile(staged)
        if _is_3mf_package(archive):
            archive.close()
            archive = None
    if archive is not None:
        with archive:
            entries = [n for n in archive.namelist() if not n.endswith("/")
                       and Path(n).suffix.lower() in {".stl", ".3mf"}]
            pick = next((n for n in entries if Path(n).name.casefold() == name.casefold()), None)
            if pick is None and len(entries) == 1: pick = entries[0]
            if pick is None:
                staged.unlink(missing_ok=True)
                raise RuntimeError(
                    f"{label} sent a zip with {len(entries)} model files and none is named "
                    f"{name}, so Print Handy cannot tell which one was chosen; nothing was kept")
            # The download cap counted the compressed bytes; the chosen member
            # is held to the same cap as it expands, counted as it is written
            # (the header's declared size is not trusted), so a small archive
            # cannot fill the disk before the intake limits see the file.
            from .intake_resources import CHUNK, MAX_DOWNLOAD_BYTES, MIB, _refuse
            try:
                if archive.getinfo(pick).file_size > MAX_DOWNLOAD_BYTES:
                    _refuse(f"file exceeds {MAX_DOWNLOAD_BYTES // MIB} MiB limit")
                with archive.open(pick) as src, dest.open("wb") as out:
                    total = 0
                    while chunk := src.read(CHUNK):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            _refuse(f"file exceeds {MAX_DOWNLOAD_BYTES // MIB} MiB limit")
                        out.write(chunk)
            except BaseException:
                dest.unlink(missing_ok=True)
                staged.unlink(missing_ok=True)
                raise
        staged.unlink(missing_ok=True)
    else:
        sent = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
        if not exact and sent.casefold() != name.casefold():
            staged.unlink(missing_ok=True)
            raise RuntimeError(
                f"{label} sent a single file ({sent or 'unnamed'}) that is not the chosen "
                f"{name}, so nothing was kept. Download the file from the model page in a "
                "browser and upload or drop it here instead.")
        staged.replace(dest)
    if dest.stat().st_size == 0:
        raise RuntimeError(f"{label} download was empty")
    return dest


def _answer_gap(label):
    return (f"{label} answered the download request, but Print Handy could not find a "
            "file URL in the answer, so nothing was downloaded. This download path has "
            "not been verified with a live login token yet. Workaround: open the model "
            "page in a browser, download the .stl/.3mf file there, then upload or drop "
            "the file here instead.")


def _title_slug(title, fallback):
    return re.sub(r'[^a-z0-9]+', '-', str(title).lower()).strip('-') or fallback


# Creative Commons codes in the order both Anycubic's and Elegoo's web
# clients enumerate them; each site's own trailing value is its house license.
_CC_LICENSES = ["CC BY", "CC BY-SA", "CC BY-NC", "CC BY-NC-SA", "CC BY-ND", "CC BY-NC-ND", "CC0"]


THANGS = "https://thangs.com"
THANGS_ANSWER_GAP = _answer_gap("Thangs")


def thangs_id(url):
    """The model id from a Thangs URL: /designer/<user>/3d-model/<title>-<id> or /m/<id>."""
    path = urllib.parse.unquote(urllib.parse.urlparse(str(url)).path)
    m = re.fullmatch(r"/m/(\d+)/?", path) or re.search(r"/3d-model/.*-(\d+)/?$", path)
    if not m: raise ValueError(f"Thangs URL needs /3d-model/<title>-<number> or /m/<number>: {url}")
    return m.group(1)


def thangs_inventory(url):
    # GET /api/models/v2/<id> is the call the model page itself makes (its
    # answer is embedded in the page's __NEXT_DATA__). A model that does not
    # exist, or is not public, answers 403 Forbidden rather than 404.
    mid = thangs_id(url)
    status, d = _web_api("GET", f"{THANGS}/api/models/v2/{mid}?likes=false")
    if status in (403, 404):
        raise RuntimeError(f"Thangs model {mid} was not found — it may have been removed or "
                           "made private, or the link is wrong")
    if status != 200 or not isinstance(d, dict) or not d.get("id"):
        raise RuntimeError(f"Thangs answered the model lookup with HTTP {status}, so the "
                           "link was not followed")
    rows = []
    for part in d.get("parts") or []:
        if not isinstance(part, dict): continue
        # name can be a display name without an extension; the stored file
        # name carries the real format.
        name = str(part.get("originalFileName") or "").strip() \
            or Path(str(part.get("filename") or "")).name
        if Path(name).suffix.lower() not in {".stl", ".3mf"}: continue
        rows.append({"id": part.get("partIdentifier"), "name": name, "bytes": part.get("size"),
                     "kind": "files"})
    title = str(d.get("name") or "").strip() or f"thangs-{mid}"
    license_file = d.get("license")
    # Thangs keeps the license as an uploaded file; its name is the best
    # summary the API gives ("Creative Commons - Attribution 4.0 ...pdf"),
    # unless it is just a generic LICENSE file.
    license_value = Path(str(license_file)).stem if license_file else None
    if license_value and license_value.casefold() in {"license", "licence"}:
        license_value = "license file on the model page"
    job = {"site": "thangs", "model_id": str(d["id"]), "title": title,
           "slug": _title_slug(title, mid), "license": license_value, "sources": rows}
    if d.get("visibility") == "paid" or d.get("plans") or d.get("marketplaceInfo"):
        job["paid"] = True
    if not rows:
        job["no_geometry_reason"] = (
            f"Thangs model \"{title}\" lists no STL/3MF files, so there is no selectable "
            "geometry to reslice")
    return job


def thangs_download(job, source, target):
    cached = _cached_source("thangs", "id", source.get("id"), job.get("model_id"))
    if cached is not None and cached.name == Path(source.get("name") or "").name:
        return _reuse_cached(cached, target, "Thangs")
    from .site_accounts import read_token, refused
    token = read_token("thangs")
    # Thangs downloads a whole model, never one part: the web client asks
    # for this signed URL and loads it. Verified live 2026-10-29 with an
    # operator token: the answer is {fileName, signedUrl, modelSource,
    # downloadId}, and signedUrl is a signed zip of the whole model, which
    # _fetch_chosen unpacks to the chosen part.
    status, d = _web_api("GET", f"{THANGS}/api/v2/models/{job['model_id']}/download-url",
                         headers={"Authorization": f"Bearer {token}"})
    if status == 401:
        raise refused("thangs")
    if status == 403:
        if job.get("paid"):
            raise RuntimeError(
                "Thangs refused the download: this model needs a Thangs membership or a "
                "purchase that the signed-in account does not have. Print Handy never buys "
                "or unlocks models; nothing was downloaded.")
        raise refused("thangs", " (403)")
    if status == 429:
        raise RuntimeError("Thangs is rate-limiting downloads right now; wait a few minutes, "
                           "then send the link again")
    if status != 200 or not isinstance(d, dict):
        raise RuntimeError(f"Thangs answered the download request with HTTP {status}; "
                           "nothing was downloaded")
    if (d.get("userDownloadData") or {}).get("downloadLimitReached"):
        raise RuntimeError("Thangs says this account reached its daily download limit; "
                           "try again tomorrow. Nothing was downloaded.")
    if d.get("fileName") == "Error":
        raise RuntimeError("Thangs says this model's download is not ready yet; try again "
                           "later. Nothing was downloaded.")
    url = _answer_url(d)
    if not url: raise RuntimeError(THANGS_ANSWER_GAP)
    single = len(job.get("sources") or []) <= 1
    return _fetch_chosen(url, target, source.get("name"), "Thangs", exact=single)


NEXPRINT = "https://www.nexprint.com/gateway"
NEXPRINT_ANSWER_GAP = _answer_gap("Nexprint")
NEXPRINT_LICENSES = _CC_LICENSES + ["Nexprint Standard License"]


def nexprint_id(url):
    """The model id from a Nexprint URL: /[<lang>/]models/<id>."""
    path = urllib.parse.urlparse(str(url)).path
    m = re.search(r"/models/([A-Za-z0-9]+)/?$", path)
    if not m: raise ValueError(f"Nexprint URL needs /models/<id>: {url}")
    return m.group(1)


def _nexprint_result(method, path, body=None, token=None):
    """One Nexprint gateway call; returns the envelope's data.

    The gateway keeps HTTP 200 for refusals and puts the cause in the
    envelope's code (401 = not signed in). Known codes are named with their
    remedy; an unknown one is reported by number, never by the site's text.
    """
    headers = {"User-Lang": "en"}
    if token: headers["Authorization"] = f"Bearer {token}"
    status, d = _web_api(method, f"{NEXPRINT}{path}", body=body, headers=headers)
    if not isinstance(d, dict) or "code" not in d:
        if token and status in (401, 403):
            from .site_accounts import refused
            raise refused("nexprint")
        raise RuntimeError(f"Nexprint answered with an unexpected response (HTTP {status}), "
                           "so the request was not followed")
    code = d.get("code")
    if code in (0, 200): return d.get("data")
    if code in (401, 902):
        from .site_accounts import refused
        raise refused("nexprint")
    if code in (1003002049, 1003002050, 1003002051):
        raise RuntimeError("Nexprint says this account reached its download limit; try again "
                           "later. Nothing was downloaded.")
    if code == 403:
        raise RuntimeError("Nexprint refused the request as unusual activity. Open the model "
                           "on nexprint.com in a browser, then send the link again later. "
                           "Print Handy does not try to bypass site checks.")
    raise RuntimeError(f"Nexprint refused the request (code {code})")


def nexprint_inventory(url):
    mid = nexprint_id(url)
    d = _nexprint_result("GET", "/api/v1/model-library-server/model-base-info/get?id="
                         + urllib.parse.quote(mid))
    # An unknown id still answers code 0, with every field null.
    if not isinstance(d, dict) or not d.get("id"):
        raise RuntimeError(f"Nexprint model {mid} was not found — it may have been removed, "
                           "or the link is wrong")
    model_id = str(d["id"])
    # Print settings ship as 3MF files that also appear in modelFileList;
    # downloading one is a type 5 request against the setting's id.
    settings_by_file = {}
    for setting in d.get("settingInfoList") or []:
        f = (setting or {}).get("settingFile") or {}
        if f.get("fileId") is not None:
            settings_by_file[str(f["fileId"])] = setting
    rows, seen = [], set()
    for f in d.get("modelFileList") or []:
        if not isinstance(f, dict) or f.get("fileId") is None: continue
        fid = str(f["fileId"])
        if fid in seen: continue
        seen.add(fid)
        name = str(f.get("fileName") or "").strip()
        if Path(name).suffix.lower() not in {".stl", ".3mf"}: continue
        setting = settings_by_file.get(fid)
        size = f.get("fileSize")
        row = {"id": fid, "name": name, "bytes": int(size) if isinstance(size, (int, float)) else None,
               "kind": "files", "file_type": "5" if setting else "2",
               "biz_id": str(setting["id"]) if setting else model_id}
        if setting and setting.get("settingName"):
            # Setting projects often share the designer file's name; the
            # picker shows the setting's name so the rows stay distinct.
            row["profile"] = str(setting["settingName"])
            row["title"] = f"{name} (print setting: {row['profile']})"
        rows.append(row)
    # The designer's own files first, then print-setting projects.
    rows.sort(key=lambda r: r["file_type"] != "2")
    title = str(d.get("modelName") or "").strip() or f"nexprint-{mid}"
    lic = d.get("licenseType")
    job = {"site": "nexprint", "model_id": model_id, "title": title,
           "slug": _title_slug(title, mid),
           "license": NEXPRINT_LICENSES[lic] if isinstance(lic, int) and 0 <= lic < len(NEXPRINT_LICENSES) else None,
           "sources": rows}
    if not rows:
        job["no_geometry_reason"] = (
            f"Nexprint model \"{title}\" lists no STL/3MF files, so there is no selectable "
            "geometry to reslice")
    return job


def nexprint_download(job, source, target):
    cached = _cached_source("nexprint", "id", source.get("id"), job.get("model_id"))
    if cached is not None and cached.name == Path(source.get("name") or "").name:
        return _reuse_cached(cached, target, "Nexprint")
    from .site_accounts import read_token
    token = read_token("nexprint")
    # Verified live 2026-10-29 with an operator token: the answer is
    # {code: 0, data: {fileInfoList: [{fileId, type, resultUrl}]}}, and
    # resultUrl is a presigned object-storage URL for that one file.
    d = _nexprint_result(
        "POST", "/api/v1/model-library-server/common/presigned-download-url",
        {"fileInfoList": [{"fileId": str(source.get("id")),
                           "type": source.get("file_type") or "2",
                           "bizId": source.get("biz_id") or job["model_id"]}]},
        token=token)
    url = _answer_url(d)
    if not url: raise RuntimeError(NEXPRINT_ANSWER_GAP)
    return _fetch_chosen(url, target, source.get("name"), "Nexprint", exact=True)


MAKERONLINE = "https://makeronline.com"
MAKERONLINE_LICENSES = _CC_LICENSES + ["Standard Digital File License"]

# MakerOnline is inventory-only, like MyMiniFactory: the model lookup and
# file list answer anonymously, but its download call (POST /api/file/download)
# also wants a Risk-Token produced by NetEase Yidun's in-browser device
# fingerprinting SDK. Tested live 2026-10-29 with a valid operator login token
# and no Risk-Token: the token was accepted (no auth refusal), and every
# download answered code 1 "The system is busy" on two different models. Faking
# that fingerprint is a bot-check bypass, which Print Handy never does, so the
# refusal below IS the download behavior and no credential changes it.
MAKERONLINE_DOWNLOAD_GAP = (
    "Print Handy cannot download files from MakerOnline. Its download service "
    "requires a browser security check (a device-fingerprint token) on every "
    "download, which Print Handy does not fake or get around; with a valid login "
    "alone the site refuses every download. Workaround: open the model page on "
    "makeronline.com in a browser, download the .stl/.3mf file there, then upload "
    "or drop the file here instead.")


def makeronline_id(url):
    """The model id from a MakerOnline URL: /[<lang>/]model/<title>/<id>.html."""
    path = urllib.parse.unquote(urllib.parse.urlparse(str(url)).path)
    m = re.search(r"/model/(?:.*/)?(\d+)\.html$", path)
    if not m: raise ValueError(f"MakerOnline URL needs /model/<title>/<number>.html: {url}")
    return m.group(1)


def makeronline_inventory(url):
    mid = makeronline_id(url)
    status, d = _web_api("GET", f"{MAKERONLINE}/api/mold/detail?id={mid}", headers={"lang": "en"})
    if not isinstance(d, dict) or "code" not in d:
        raise RuntimeError(f"MakerOnline answered with an unexpected response (HTTP {status}), "
                           "so the link was not followed")
    if d.get("code") != 0:
        if "not exist" in str(d.get("message") or ""):
            raise RuntimeError("MakerOnline says this model does not exist — it may have been "
                               "removed, or the link is wrong")
        raise RuntimeError(f"MakerOnline refused the model lookup (code {d.get('code')})")
    data = d.get("data")
    if not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError(f"MakerOnline model {mid} was not found — it may have been removed, "
                           "or the link is wrong")
    rows = []
    for f in data.get("files") or []:
        if not isinstance(f, dict) or not f.get("key"): continue
        name = str(f.get("file_name") or "").strip()
        if Path(name).suffix.lower() not in {".stl", ".3mf"}: continue
        rows.append({"id": f["key"], "name": name, "bytes": f.get("file_size"),
                     "kind": "files", "file_id": f.get("file_id")})
    title = str(data.get("title") or "").strip() or f"makeronline-{mid}"
    lic = data.get("license")
    job = {"site": "makeronline", "model_id": str(data["id"]), "title": title,
           "slug": _title_slug(title, mid),
           "license": MAKERONLINE_LICENSES[lic - 1] if isinstance(lic, int) and 1 <= lic <= len(MAKERONLINE_LICENSES) else None,
           "sources": rows, "download_gap": MAKERONLINE_DOWNLOAD_GAP}
    if not rows:
        job["no_geometry_reason"] = (
            f"MakerOnline model \"{title}\" lists no STL/3MF model files, so there is no "
            "selectable geometry to reslice — Print Handy never uses a site's printer-specific "
            "print profiles or pre-sliced G-code")
    return job


def makeronline_download(job, source, target):
    # Inventory-only: never touches the network (see MAKERONLINE_DOWNLOAD_GAP).
    raise RuntimeError(MAKERONLINE_DOWNLOAD_GAP)



# ---------------------------------------------------------------------------
# Direct file links (#607): a public link straight at a print file, on any
# host — the case the known-site providers cannot cover, e.g. a file too big
# for Discord shared as a Dropbox link. This is the first arbitrary-URL fetch
# in intake, so the fetch is guarded: the destination is checked to be a
# public address before connecting and again after every redirect (redirects
# are followed manually so no hop skips the check), the download streams
# under the same intake size limit as site downloads (intake_resources.
# save_download), and the saved bytes are sniffed before the usual
# check_source/audit path parses them — the extension is never trusted.
DIRECT_FILE_EXTS = {".stl", ".3mf"}  # mirrors prepared_intake.MODEL_FILE_EXTS and the site inventory filters
DIRECT_MAX_REDIRECTS = 5
SUPPORTED_LINKS_MESSAGE = (
    "supported links: MakerWorld, Printables, Thingiverse, MyMiniFactory, Cults3D, Creality Cloud, Thangs, Nexprint, MakerOnline "
    "model pages, or a direct link to a .stl/.3mf file (a Dropbox share link works too)")


class DirectLinkError(RuntimeError):
    """Curated, operator-facing refusal for a direct file link.

    Like the other providers' plain RuntimeErrors, the text names the cause
    and the remedy; prepared intake posts it as the failure reason instead of
    the generic intake wording, so chat hears what actually went wrong."""


def _direct_name_from_path(path):
    """The file name when a URL path ends in an accepted geometry extension, else None."""
    name = Path(urllib.parse.unquote(str(path or ""))).name
    return name if Path(name).suffix.lower() in DIRECT_FILE_EXTS else None


def dropbox_fetch_url(url):
    """The download form of a Dropbox share link, or None when url is not one.

    A /s/ or /scl/ share link serves an HTML preview page; asking for dl=1
    (or using a dl.dropboxusercontent.com link as-is) serves the file itself.
    """
    parts = urllib.parse.urlsplit(str(url))
    host = (parts.hostname or "").lower()
    if host == "dl.dropboxusercontent.com":
        return str(url)
    if host not in {"www.dropbox.com", "dropbox.com"}:
        return None
    if not (parts.path.startswith("/s/") or parts.path.startswith("/scl/")):
        return None
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query["dl"] = "1"
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def direct_link_target(url):
    """(fetch_url, name_hint_or_None) when url is a candidate direct file link, else None."""
    parts = urllib.parse.urlsplit(str(url))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    fetch = dropbox_fetch_url(url)
    if fetch is not None:
        return fetch, _direct_name_from_path(parts.path)
    name = _direct_name_from_path(parts.path)
    if name is None:
        return None
    return str(url), name


def _resolve_host_ips(host):
    """Every address a hostname resolves to (module-level so tests can fake DNS)."""
    return {info[4][0] for info in socket.getaddrinfo(host, None)}


def _refuse_non_public(host):
    raise DirectLinkError(
        f"That link points at {host}, which is not a public address, so Print Handy "
        "will not fetch it. Direct file links must be public links anyone on the "
        "internet can open. Nothing was downloaded.")


def assert_public_url(url):
    """Refuse a fetch URL whose destination is not a public address.

    Runs before the first connection and again on every redirect target:
    a redirect to localhost or a private address is the standard way to
    smuggle one past a check on the original URL."""
    parts = urllib.parse.urlsplit(str(url))
    if parts.scheme not in ("http", "https"):
        raise DirectLinkError(
            "Direct file links must start with http:// or https://. Nothing was downloaded.")
    if parts.username or parts.password:
        raise DirectLinkError(
            "That link has a user name or password in it, so Print Handy will not fetch "
            "it. Share the file with a public link instead. Nothing was downloaded.")
    host = parts.hostname
    if not host:
        raise DirectLinkError(
            "That link has no host name, so it cannot be fetched. Nothing was downloaded.")
    if host.lower() == "localhost":
        _refuse_non_public(host)
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            ips = [ipaddress.ip_address(ip) for ip in _resolve_host_ips(host)]
        except (OSError, UnicodeError):
            ips = []
        if not ips:
            raise DirectLinkError(
                f"Print Handy could not look up the host {host}, so the file was not "
                "downloaded. Check the link, or check the network connection, then "
                "send the link again.") from None
    if any(not ip.is_global for ip in ips):
        _refuse_non_public(host)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirects are followed manually so every hop re-runs assert_public_url."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_once(url):
    """One GET with no redirect following (module-level so tests can fake the transport)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.build_opener(_NoRedirectHandler()).open(req, timeout=90)


_REDIRECT_CODES = (301, 302, 303, 307, 308)


def _fetch_direct_response(url):
    """(response, final_url) for a direct file link, following redirects manually.

    Every hop — the original URL and each redirect target — passes
    assert_public_url before anything connects to it."""
    current = str(url)
    for hop in range(DIRECT_MAX_REDIRECTS + 1):
        assert_public_url(current)
        try:
            response = _open_once(current)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            code = exc.code
            exc.close()
            if code in _REDIRECT_CODES and location:
                if hop >= DIRECT_MAX_REDIRECTS:
                    raise DirectLinkError(
                        "That link redirected too many times, so the file was not "
                        "downloaded. Send a link that points at the file more "
                        "directly. Nothing was downloaded.") from None
                current = urllib.parse.urljoin(current, location)
                continue
            raise DirectLinkError(
                f"The host for that link answered with HTTP {code}, so the file was "
                "not downloaded. Check the link is public and the file is still "
                "there, then send it again.") from None
        except (urllib.error.URLError, OSError):
            raise DirectLinkError(
                "Print Handy could not reach the host for that link, so the file "
                "was not downloaded. Check the link, or check the network "
                "connection, then send the link again.") from None
        status = getattr(response, "status", None) or getattr(response, "code", 200)
        if status in _REDIRECT_CODES:
            location = response.headers.get("Location") if getattr(response, "headers", None) else None
            response.close()
            if location and hop < DIRECT_MAX_REDIRECTS:
                current = urllib.parse.urljoin(current, location)
                continue
            raise DirectLinkError(
                "That link redirected too many times, so the file was not downloaded. "
                "Send a link that points at the file more directly. Nothing was downloaded.")
        return response, current
    raise DirectLinkError(  # pragma: no cover - the loop always returns or raises
        "That link redirected too many times, so the file was not downloaded.")


def _content_disposition_name(headers):
    """The file name a response offers via Content-Disposition, else None."""
    if headers is None:
        return None
    raw = headers.get("Content-Disposition")
    if not raw:
        return None
    match = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", str(raw))
    if match:
        name = urllib.parse.unquote(match.group(1).strip().strip('"'))
    else:
        match = re.search(r'filename\s*=\s*(?:"([^"]+)"|([^;]+))', str(raw))
        if not match:
            return None
        name = (match.group(1) or match.group(2)).strip()
    return Path(name).name or None


def _sniff_direct_file(path):
    """Refuse bytes that are plainly not the claimed geometry, before audit parses them.

    An HTML body means the link served a share/consent page, not the file; a
    3MF is a zip archive, so anything else under that name is not one. The
    file is removed either way: nothing is left behind as a model."""
    with Path(path).open("rb") as f:
        head = f.read(512)
    if head.lstrip().startswith(b"<"):
        Path(path).unlink(missing_ok=True)
        raise DirectLinkError(
            "That link returned a web page instead of a print file, so nothing was "
            "saved as a model. The link has to end at the file itself: for a file "
            "host, use its direct download link; a Dropbox share link works as-is.")
    if Path(path).suffix.lower() == ".3mf" and not head.startswith(b"PK\x03\x04"):
        Path(path).unlink(missing_ok=True)
        raise DirectLinkError(
            "That link's file is not a 3MF file (a 3MF is a zip archive), so it was "
            "not saved. Check the link points at the model's .3mf file itself.")


def direct_inventory(url):
    target = direct_link_target(url)
    if target is None:
        raise ValueError(SUPPORTED_LINKS_MESSAGE)
    fetch_url, name = target
    host = (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    stem = Path(name).stem if name else "shared-file"
    slug = safe_slug(stem) or "direct-file"
    source = {"n": 1, "name": name or slug, "title": stem, "kind": "direct",
              "download_url": fetch_url, "source_url": str(url), "host": host}
    return {"site": "direct", "model_id": slug, "title": stem, "slug": slug,
            "license": None, "summary": "", "sources": [source]}


def direct_download(job, source, target):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    fetch_url = (source or {}).get("download_url") or (job or {}).get("url")
    if not fetch_url:
        raise DirectLinkError("That direct link has no address to fetch. Send the link again.")
    response, final_url = _fetch_direct_response(fetch_url)
    with response:
        headers = getattr(response, "headers", None)
        content_type = str(headers.get("Content-Type") or "") if headers else ""
        if "text/html" in content_type.lower():
            raise DirectLinkError(
                "That link returned a web page instead of a print file, so nothing "
                "was saved as a model. The link has to end at the file itself: for "
                "a file host, use its direct download link; a Dropbox share link "
                "works as-is.")
        name = (_content_disposition_name(headers)
                or _direct_name_from_path(urllib.parse.urlsplit(final_url).path)
                or (source or {}).get("name"))
        if not name or Path(str(name)).suffix.lower() not in DIRECT_FILE_EXTS:
            raise DirectLinkError(
                f"That link gave back {name or 'an unnamed file'}, which is not a "
                "print file Print Handy can use. Direct links must serve a .stl or "
                ".3mf file. Nothing was saved.")
        dest = target / Path(str(name)).name
        from .intake_resources import save_download
        save_download(response, dest)  # streams under the shared intake size limit
    if dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise DirectLinkError("That link downloaded as an empty file, so nothing was saved.")
    _sniff_direct_file(dest)
    return dest

# Per-site registry (#462): site -> function NAME, resolved through this module's globals
# at call time so existing tests that monkeypatch maker_*/printables_*/thingiverse_*/
# myminifactory_* by name keep working exactly as before.
SITE_INVENTORY_FUNCS={"makerworld":"maker_inventory","printables":"printables_inventory",
                      "thingiverse":"thingiverse_inventory",
                      "myminifactory":"myminifactory_inventory",
                      "cults3d":"cults_inventory",
                      "crealitycloud":"crealitycloud_inventory",
                      "thangs":"thangs_inventory","nexprint":"nexprint_inventory",
                      "makeronline":"makeronline_inventory",
                      "direct":"direct_inventory"}
SITE_DOWNLOAD_FUNCS={"makerworld":"maker_download","printables":"printables_download",
                     "thingiverse":"thingiverse_download",
                     "myminifactory":"myminifactory_download",
                     "cults3d":"cults_download",
                     "crealitycloud":"crealitycloud_download",
                     "thangs":"thangs_download","nexprint":"nexprint_download",
                     "makeronline":"makeronline_download",
                     "direct":"direct_download"}
SITE_HOSTS={"makerworld.com":"makerworld","www.makerworld.com":"makerworld",
            "printables.com":"printables","www.printables.com":"printables",
            "thingiverse.com":"thingiverse","www.thingiverse.com":"thingiverse",
            "myminifactory.com":"myminifactory","www.myminifactory.com":"myminifactory",
            "cults3d.com":"cults3d","www.cults3d.com":"cults3d",
            "crealitycloud.com":"crealitycloud","www.crealitycloud.com":"crealitycloud",
            "thangs.com":"thangs","www.thangs.com":"thangs",
            "nexprint.com":"nexprint","www.nexprint.com":"nexprint",
            "makeronline.com":"makeronline","www.makeronline.com":"makeronline"}
def inventory_for_url(url):
    site=SITE_HOSTS.get((urllib.parse.urlparse(url).hostname or "").lower())
    if site is None:
        if direct_link_target(url) is not None: return direct_inventory(url)
        raise ValueError(SUPPORTED_LINKS_MESSAGE)
    return globals()[SITE_INVENTORY_FUNCS[site]](url)
def download_for_site(job, source, target):
    name=SITE_DOWNLOAD_FUNCS.get((job or {}).get("site"))
    if name is None: raise ValueError(SUPPORTED_LINKS_MESSAGE)
    return globals()[name](job, source, target)


def _timing(job_path, stage, **extra):
    """Launch latency instrumentation: append one line to <job>/TIMING.jsonl. Never raises."""
    try:
        line={"stage":stage,"t":round(time.time(),3),"pid":os.getpid(),**extra}
        logger.debug("TIMING %s", json.dumps(line))  # stdout stays clean in production logs
        if job_path is not None:
            with (Path(job_path).parent/"TIMING.jsonl").open("a") as out: out.write(json.dumps(line)+"\n")
    except Exception:
        pass


def file_sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source,"sha256").hexdigest()


def zmod_md5_valid(path):
    """Validate ZMod preprocess format: first-line digest covers remaining bytes."""
    data = Path(path).read_bytes()
    first, sep, payload = data.partition(b"\n")
    match = re.fullmatch(rb"; MD5:([0-9a-fA-F]{32})\r?", first)
    if not sep or not match:
        return False
    return hashlib.md5(payload).hexdigest().encode("ascii") == match.group(1).lower()


def finalize_multicolor(*args, **kwargs):
    """ZMod color/slot-map insertion and MD5, through the printer adapter."""
    return printers.current().native.finalize(*args, **kwargs)


def native_heater_lines_valid(text, bed, hotend):
    """Require the documented native startup prefix, not later layer heaters."""
    valid = printers.current().native.native_startup_valid
    return valid(text, bed, hotend)


def plate_manifest(path):
    """Inventory source plate assignments without slicing or touching the printer."""
    path=Path(path)
    if path.suffix.lower() != ".3mf": raise RuntimeError("plate manifest requires a 3MF source")
    with zipfile.ZipFile(path) as archive:
        names=set(archive.namelist())
        preview_ids={int(m.group(1)) for name in names
                     if (m:=re.fullmatch(r"Metadata/plate_(\d+)\.png",name))}
        if "Metadata/model_settings.config" not in names:
            raise RuntimeError("3MF has no plate assignments in Metadata/model_settings.config")
        try:
            root=ET.fromstring(archive.read("Metadata/model_settings.config"))
            plates=[]
            for plate in root.iter("plate"):
                metadata={m.get("key"):m.get("value") for m in plate.findall("metadata")}
                raw_id=metadata.get("plater_id")
                if raw_id is None: raise RuntimeError("plate has no plater_id assignment")
                plate_id=int(raw_id)
                object_ids=[]
                for instance in plate.findall("model_instance"):
                    for entry in instance.findall("metadata"):
                        if entry.get("key")=="object_id":
                            value=entry.get("value")
                            if value is None: raise RuntimeError(f"plate {plate_id} has empty object assignment")
                            object_ids.append(int(value))
                if not object_ids: raise RuntimeError(f"plate {plate_id} has no object assignments")
                plates.append({"id":plate_id,"name":metadata.get("plater_name") or f"Plate {plate_id}",
                               "object_ids":object_ids,"status":"planned"})
        except (ET.ParseError,KeyError,TypeError,ValueError) as exc:
            raise RuntimeError(f"invalid 3MF plate assignments: {exc}") from exc
        ids=[p["id"] for p in plates]
        if not ids or len(ids)!=len(set(ids)) or (preview_ids and preview_ids!=set(ids)):
            raise RuntimeError("3MF plate assignments missing, duplicated, or inconsistent with previews")
    return {"source_file":str(path.resolve()),"source_sha256":file_sha256(path),
            "plates":sorted(plates,key=lambda p:p["id"])}


class NoPrintableVolume(RuntimeError):
    """The mesh encloses no volume (flat or degenerate): nothing to slice or print (#194)."""


def model_info_output(path):
    """Run or read cached geometry extents/manifoldness for a model file."""
    path = Path(path)
    global _INFO_CACHE
    if "_INFO_CACHE" not in globals():
        _INFO_CACHE = {}
    try:
        st = path.stat()
        key = (str(path.resolve()), st.st_size, st.st_mtime)
        if key in _INFO_CACHE:
            return _INFO_CACHE[key]
    except Exception:
        key = None

    # Measured from the printable mesh in-process (Orca-only stack; no Prusa).
    # Cutters and modifiers are already excluded by source_triangles.
    from .orca_adapter import source_triangles
    tris = source_triangles(path)
    if not tris:
        raise RuntimeError("model has no printable geometry")
    from .mesh_fast import bounds_and_volume
    lo, hi, vol = bounds_and_volume(tris)
    if min(hi[a] - lo[a] for a in range(3)) <= 1e-6 or abs(vol) <= 1e-9:
        # Flat or zero-volume mesh (#194): nothing for a slicer to fill, so refuse before
        # any plate is offered rather than slicing empty layers and starting a print.
        raise NoPrintableVolume("model has no printable volume (flat or zero-volume mesh)")
    info = "".join(f"{k} = {hi[a] - lo[a]:.2f}\n" for a, k in enumerate(("size_x", "size_y", "size_z")))
    info += f"volume = {abs(vol):.2f}\n"
    if key is not None:
        _INFO_CACHE[key] = info
    return info


def mesh_topology(triangles):
    """Conservative positional topology; no repair or self-intersection test.

    Weld coordinates on a six-decimal grid. Exclude faces whose squared
    double-area is <= 1e-18 source-units**4 from all topology adjacency.
    """
    if hasattr(triangles, '__len__') and len(triangles) > 100_000:
        from .compact_topology import mesh_topology_compact
        return mesh_topology_compact(triangles)
    vertices = {}
    faces = []
    degenerate = 0
    for tri in triangles:
        points = [tuple(float(v) for v in point) for point in tri]
        ids = []
        for point in points:
            key = tuple(round(v, 6) for v in point)
            ids.append(vertices.setdefault(key, len(vertices)))
        u = tuple(b - a for a, b in zip(points[0], points[1]))
        v = tuple(b - a for a, b in zip(points[0], points[2]))
        cross = (u[1]*v[2] - u[2]*v[1], u[2]*v[0] - u[0]*v[2],
                 u[0]*v[1] - u[1]*v[0])
        if len(set(ids)) < 3 or sum(c*c for c in cross) <= 1e-18:
            degenerate += 1
            continue
        faces.append(ids)

    edges = {}
    vertex_faces = {}
    adjacency = [[] for _ in faces]
    vertex_links = {}
    for fi, face in enumerate(faces):
        for vertex in face:
            vertex_faces.setdefault(vertex, set()).add(fi)
        for a, b in zip(face, face[1:] + face[:1]):
            edges.setdefault((min(a, b), max(a, b)), []).append((fi, a < b))
    singular_ids = set()
    for edge, uses in edges.items():
        # A star preserves shell connectivity with k-1 links, even for a dense
        # non-manifold fan. Never expand k*(k-1) face pairs (including k > 64).
        first = uses[0][0]
        for other, _ in uses[1:]:
            adjacency[first].append(other)
            adjacency[other].append(first)
        if len(uses) != 2:
            singular_ids.update(edge)
        else:
            a, b = uses[0][0], uses[1][0]
            for vertex in edge:
                links = vertex_links.setdefault(vertex, {})
                links.setdefault(a, []).append(b)
                links.setdefault(b, []).append(a)

    seen = set()
    shells = 0
    for start in range(len(faces)):
        if start in seen:
            continue
        shells += 1
        stack = [start]
        seen.add(start)
        while stack:
            for other in adjacency[stack.pop()]:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)

    # A closed edge neighborhood can still contain disconnected face fans,
    # e.g. two tetrahedra touching at one vertex. Traverse only local links,
    # not every edge for every vertex, so this remains linear in mesh size.
    for vertex, attached in vertex_faces.items():
        if vertex in singular_ids:
            continue
        links = vertex_links.get(vertex, {})
        reached = {next(iter(attached))}
        stack = list(reached)
        while stack:
            for other in links.get(stack.pop(), ()):
                if other not in reached:
                    reached.add(other)
                    stack.append(other)
        if len(reached) != len(attached):
            singular_ids.add(vertex)

    boundary = sum(len(uses) == 1 for uses in edges.values())
    nonmanifold = sum(len(uses) > 2 for uses in edges.values())
    winding = sum(len(uses) == 2 and uses[0][1] == uses[1][1] for uses in edges.values())
    singular = len(singular_ids)
    manifold = not (boundary or nonmanifold or singular or degenerate or winding)
    count = len(faces) + degenerate
    return {"format": "triangle-mesh", "triangles": count,
            "shells": shells, "triangle_count": count, "connected_shells": shells,
            "boundary_edges": boundary, "non_manifold_edges": nonmanifold,
            "non_manifold_vertices": singular, "singular_vertices": singular, "winding_inconsistencies": winding,
            "degenerate_triangles": degenerate, "watertight": boundary == 0 and nonmanifold == 0,
            "manifold": manifold}


def mesh_audit(path, printer_model=None):
    """Read-only, versioned topology report for STL/3MF source geometry."""
    from .orca_adapter import source_triangles
    triangles = source_triangles(path)
    if not triangles:
        raise RuntimeError("model has no printable geometry")
    from .compact_geometry import IndexedTriangles
    if isinstance(triangles, IndexedTriangles):
        from .mesh_fast import bounds_and_volume
        lo, hi, _ = bounds_and_volume(triangles)
    else:
        lo = [min(point[axis] for triangle in triangles for point in triangle) for axis in range(3)]
        hi = [max(point[axis] for triangle in triangles for point in triangle) for axis in range(3)]
    source_sha256 = file_sha256(path)
    from .orca_adapter import placement_candidates
    return {"schema_version": 1, "sha256": source_sha256,
            "source_sha256": source_sha256,
            "dimensions_mm": [round(hi[axis] - lo[axis], 2) for axis in range(3)],
            "units": "mm", "printer_model": printer_model,
            "placement_candidates": placement_candidates(path, printer_model=printer_model),
            **mesh_topology(triangles)}


def audit(path, printer_model=None):
    from .intake_resources import check_source
    check_source(path)
    with open(path,"rb") as source:
        digest=hashlib.file_digest(source,"sha256").hexdigest()
    out={"path":path.name,"bytes":path.stat().st_size,"sha256":digest,"kind":path.suffix.lower()}
    if path.suffix.lower()==".3mf":
        with zipfile.ZipFile(path) as z:
            names=z.namelist(); out["archive_files"]=len(names)
            plate_ids={int(m.group(1)) for n in names if (m:=re.search(r'plate_(\d+)\.(?:gcode|png)$',n))}
            if "Metadata/model_settings.config" in names:
                try:
                    settings = ET.fromstring(z.read("Metadata/model_settings.config"))
                    for plate in settings.iter("plate"):
                        for metadata in plate.iter("metadata"):
                            if metadata.get("key") == "plater_id":
                                plate_id = metadata.get("value")
                                if plate_id is None: raise ValueError("plate has no plater_id value")
                                plate_ids.add(int(plate_id))
                except (ET.ParseError, ValueError) as exc:
                    raise RuntimeError(f"unable to inspect 3MF plate assignments: {exc}") from exc
            out["plates"]=len(plate_ids); out["plate_ids"]=sorted(plate_ids)
            text=""
            for n in names:
                if n.endswith(("3dmodel.model","model_settings.config")):
                    try:text+=z.read(n).decode(errors="ignore")
                    except:pass
            out["objects"]=len(re.findall(r'<object\b',text)); out["components"]=len(re.findall(r'<component\b',text)); out["build_items"]=len(re.findall(r'<item\b',text)); out["named_parts"]=re.findall(r'<metadata key="name" value="([^"]+)',text)[:30]
    from .source_colors import inventory, prompts
    manifest = inventory(path)
    out['color_manifest'] = manifest
    out['color_prompts'] = prompts(manifest)
    try:
        out["mesh_audit"] = mesh_audit(path, printer_model=printer_model)
    except Exception as exc:
        out["mesh_audit_error"] = str(exc)
    try:
        info = model_info_output(path)
    except Exception as exc:  # unreadable mesh: slice_job re-measures and refuses clearly
        info = ""; out["geometry_error"] = str(exc)
        out["no_printable_volume"] = isinstance(exc, NoPrintableVolume)
    for key in ("size_x","size_y","size_z","number_of_parts","manifold"):
        m=re.search(rf'^{key}\s*=\s*(.+)$',info,re.M)
        if m: out[key]=m.group(1).strip()
    return out


def safe_slug(s): return re.sub(r'[^a-z0-9]+','-',s.lower()).strip('-')[:70]
def intake_id_from_message(source, message_id):
    """Return the canonical intake identity for a Discord message, never its snowflake."""
    if not message_id:
        raise ValueError('plain model link requires a stable Discord message ID')
    identity = [getattr(source, 'profile', ''), str(getattr(source, 'chat_id', '')),
                str(getattr(source, 'thread_id', '') or ''), str(message_id)]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def intake_folder(archive, slug, intake_id=None):
    if intake_id is not None and not re.fullmatch(r'[a-f0-9]{64}', intake_id):
        raise ValueError('intake ID must be a 64 character lowercase SHA256')
    return Path(archive) / (safe_slug(slug) + ('--' + intake_id if intake_id else ''))


def write_job(folder, job):
    # Atomic replace: the background slice and the intake flow read JOB.json
    # concurrently; a plain write_text truncates first, so a reader could see
    # an empty file and fail with "Expecting value: line 1 column 1".
    target = Path(folder) / "JOB.json"
    tmp = target.with_name(f".JOB.json.{os.getpid()}.{time.monotonic_ns()}.tmp")
    tmp.write_text(json.dumps(job, indent=2) + "\n")
    os.replace(tmp, target)
def show(job):
    print(f"{job['title']} [{job['site']} {job['model_id']}]\nHardware: {printer_state.summary(job['hardware'])}")
    print("Sources:")
    for x in job["sources"]:
        if len(x.get('filaments', [])) > 1 or len(x.get('source_parts', [])) > 1:
            print('  Color/part choices required for this profile: ' + json.dumps({
                'filaments': x.get('filaments'), 'parts_by_plate': x.get('source_parts')}))
        if job["site"]=="makerworld":
            from .makerworld_profiles import selection_summary
            print(f"  {x['n']}. {selection_summary(x)} | nozzle {x.get('site_nozzle_mm')} mm | {', '.join(x['materials']) or 'material unstated'}")
        else: print(f"  {x['n']}. {x['name']} | {x['kind']} | {x['bytes']/1048576:.1f} MB")

NUMBER=r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?'


_GCODE_LINE_RE = re.compile(r'(?im)^[ \t]*(G90|G91|G0?0|G0?1|G0?2|G0?3|G92|G28|G53|FORCE_MOVE|MANUAL_STEPPER|STEPPER_BUZZ|SET_KINEMATIC_POSITION)(?=\s|$)([^;\r\n]*)')
_GCODE_XYZ_RE = re.compile(r'([XYZ])\s*('+NUMBER+r')')
_GCODE_SUPPORTED = {"G0", "G00", "G1", "G01"}
_GCODE_FORBIDDEN = {"G28", "G53", "FORCE_MOVE", "MANUAL_STEPPER", "STEPPER_BUZZ", "SET_KINEMATIC_POSITION"}


def gcode_motion_bounds(text):
    """Return fail-closed XYZ bounds for ordinary slicer G-code."""
    absolute = True
    position = {axis: float("nan") for axis in "XYZ"}
    low = {axis: float("inf") for axis in "XYZ"}
    high = {axis: float("-inf") for axis in "XYZ"}
    recognized = 0
    for match in _GCODE_LINE_RE.finditer(text):
        command = match.group(1).upper()
        if command == "G90":
            absolute = True
        elif command == "G91":
            absolute = False
        elif command in _GCODE_FORBIDDEN:
            lineno = text.count("\n", 0, match.start()) + 1
            raise RuntimeError(f"line {lineno}: unsupported motion-affecting command {command}")
        elif command in ("G2", "G02", "G3", "G03"):
            lineno = text.count("\n", 0, match.start()) + 1
            raise RuntimeError(f"line {lineno}: arc motion is unsupported by the bounds verifier")
        elif command == "G92":
            if _GCODE_XYZ_RE.search(match.group(2).upper()):
                lineno = text.count("\n", 0, match.start()) + 1
                raise RuntimeError(f"line {lineno}: G92 changes XYZ coordinates; bounds are ambiguous")
        elif command in _GCODE_SUPPORTED:
            words = match.group(2).upper()
            found = False
            for xyz_match in _GCODE_XYZ_RE.finditer(words):
                axis, number = xyz_match.groups()
                found = True
                value = float(number)
                if absolute:
                    position[axis] = value
                else:
                    if position[axis] != position[axis]:
                        lineno = text.count("\n", 0, match.start()) + 1
                        raise RuntimeError(f"line {lineno}: relative {axis} motion before an absolute {axis} position")
                    position[axis] += value
                low[axis] = min(low[axis], position[axis])
                high[axis] = max(high[axis], position[axis])
            if found:
                recognized += 1
    if not recognized:
        raise RuntimeError("no bounded XYZ motion found")
    missing = [axis for axis in "XYZ" if low[axis] == float("inf")]
    if missing:
        raise RuntimeError("missing bounded motion for axis " + ", ".join(missing))
    return {axis.lower(): (low[axis], high[axis]) for axis in "XYZ"}


BOUNDS_CACHE = Path(os.environ.get("PRINT_HANDY_BOUNDS_CACHE", settings.state_dir()/"gcode-bounds-cache"))


def cached_motion_bounds(raw, text=None):
    """gcode_motion_bounds keyed by content sha256.

    Slicing verifies offline first; print-now then reverifies the same bytes.
    Reparsing a 24 MB plate costs ~30 s per pass, so identical bytes reuse the
    stored result. Failures are never cached, so a bad file still fails closed.
    """
    digest = hashlib.sha256(raw).hexdigest()
    entry = BOUNDS_CACHE/f"{digest}.json"
    try:
        hit = json.loads(entry.read_text())
        return {axis: tuple(hit[axis]) for axis in "xyz"}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    bounds = gcode_motion_bounds(text if text is not None else raw.decode(errors="ignore"))
    try:
        BOUNDS_CACHE.mkdir(parents=True, exist_ok=True)
        tmp = entry.with_suffix(".tmp")
        tmp.write_text(json.dumps({axis: list(bounds[axis]) for axis in "xyz"}))
        tmp.replace(entry)
    except OSError:
        pass
    return bounds


FILAMENT_SENSOR_OBJECTS = ("filament_switch_sensor head_switch_sensor", "zmod_color", "zmod_ifs", "print_stats", "save_variables")
MAP_PREFIX = "; hermes_tool_slots = "


def format_color_title(c):
    return ' '.join(w.upper() if w.casefold() in {'pla', 'pla+', 'petg', 'petg-cf', 'abs', 'tpu'} else w.capitalize() for w in str(c or '').split())


def _build_position_labels(selections, noun):
    counts = {}
    for s in selections:
        c_title = format_color_title(s.get('color'))
        mat = printer_state.display_material(s.get('material', ''))
        name = c_title if mat and c_title.casefold().endswith(mat.casefold()) else f'{c_title} {mat}'.strip()
        counts[name] = counts.get(name, 0) + 1
    labels = []
    for s in selections:
        c_title = format_color_title(s.get('color'))
        mat = printer_state.display_material(s.get('material', ''))
        name = c_title if mat and c_title.casefold().endswith(mat.casefold()) else f'{c_title} {mat}'.strip()
        if counts[name] > 1:
            labels.append(f'{name} ({noun} {s["slot"]})')
        else:
            labels.append(name)
    return labels


def build_slot_labels(selections):
    return _build_position_labels(selections, 'Slot')


def build_tool_labels(selections):
    """Choice labels for a multi_tool printer: positions are toolheads, never slots (#346)."""
    return _build_position_labels(selections, 'Toolhead')


def position_limit(adapter=None):
    """Highest colour position a selection may name on the printer.

    An IFS printer bounds selections by [printer] filament_slots; a multi_tool
    printer's positions are its toolheads (the adapter's slot_count) even
    though filament_slots stays 1 there, so the bound is whichever the printer
    actually has. Every other printer keeps exactly the bound it has today.
    """
    limit = FILAMENT_SLOTS
    try:
        limit = max(limit, int((adapter or printers.current()).slot_count or 1))
    except Exception:
        pass
    return limit


def validate_filament_selection(selection, spool_optional=False, limit=None):
    """Validate a job-bound, user-confirmed filament selection.

    `spool_optional` admits a selection with no Spoolman ID; only a single-extruder printer's unbound spool
    gets it (resolve_filament_selection, #263). `limit` is the printer's colour-position bound
    (position_limit); the default keeps the configured IFS bound.
    """
    if limit is None:
        limit = FILAMENT_SLOTS
    if not isinstance(selection, dict):
        raise ValueError("explicit filament selection is required (use 'filament-select' command)")
    slot = selection.get("slot")
    if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= limit:
        raise ValueError(f"filament slot must be an integer from 1 through {limit}")
    for key in ("color", "material"):
        if not isinstance(selection.get(key), str) or not selection[key].strip():
            raise ValueError(f"filament {key} is required")
    spool_id = selection.get("spoolman_id")
    if not (spool_id is None and spool_optional) and (
            not isinstance(spool_id, int) or isinstance(spool_id, bool) or spool_id <= 0):
        raise ValueError("a positive Spoolman spool ID is required")
    return {"slot": slot, "color": selection["color"].strip(),
            "material": selection["material"].strip(), "spoolman_id": spool_id}


def configured_slots(state):
    """The slot records intake offers, keyed by slot number.

    A recorded `filament_slots` map is used as is. A single-extruder printer with no slot map has one slot,
    its configured spool (#263): slot entries are only written from Spoolman, so an offline printer never
    gets one. Printers whose slots are bound in the record (AD5X IFS, stock Flashforge) still need their
    recorded map. A multi_slot printer whose feeder reports its own contents (Bambu AMS, #671) is not
    record-bound either: its record carries the one configured spool, offered as slot 1 the #263 way,
    and the printer's reported trays are matched to a job's filament slots at print start instead.
    """
    mappings = (state or {}).get("filament_slots") or {}
    current = printers.current()
    if mappings or (current.slot_count != 1 and current.slots_bound_by_record):
        return mappings
    spool = (state or {}).get("spool") or {}
    material = spool.get("material")
    if not isinstance(material, str) or not material.strip():
        return {}
    # The spool's recorded label is its color name; "loaded" names it without inventing a color.
    color = next((v.strip() for v in (spool.get("color"), spool.get("name")) if isinstance(v, str) and v.strip()),
                 "loaded")
    return {"1": {"color": color, "material": material.strip(), "spoolman_id": spool.get("spoolman_id")}}


def resolve_filament_selection(state, selection):
    """Resolve metadata from the requested slot, never the globally active spool.

    Configured slot labels are consistency checks, not proof of physical loading.
    An unmapped slot requires an explicit spool ID rather than an inferred one. A single-extruder
    printer's slot whose spool has no Spoolman binding needs none (#263); Spoolman is advisory (#235).
    """
    limit = position_limit()
    if not isinstance(selection, dict):
        return validate_filament_selection(selection, limit=limit)
    selection = dict(selection)
    slot = selection.get("slot")
    if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= limit:
        raise ValueError(f"filament slot must be an integer from 1 through {limit}")
    mappings = configured_slots(state)
    configured = mappings.get(str(slot), {}) if isinstance(mappings, dict) else {}
    if not isinstance(configured, dict):
        raise ValueError(f"slot {slot} configured mapping is invalid")
    mapped_id = configured.get("spoolman_id")
    # "Unbound" = the slot's spool is the printer's one configured spool (the
    # configured_slots synthesis), not a per-slot record binding: a printer
    # whose slots are not bound in the record (single-extruder models, and the
    # Bambu AMS models since #671) needs no Spoolman ID for it.
    unbound = bool(configured) and mapped_id is None and (
        printers.current().slot_count == 1 or not printers.current().slots_bound_by_record)
    if selection.get("spoolman_id") is None:
        if mapped_id is None and not unbound:
            raise ValueError(f"slot {slot} requires an explicit Spoolman spool ID; no configured mapping")
        selection["spoolman_id"] = mapped_id
    for key in ("color", "material"):
        if selection.get(key) is None and configured.get(key) is not None:
            selection[key] = configured[key]
    selection = validate_filament_selection(selection, spool_optional=unbound, limit=limit)
    if mapped_id is not None and selection["spoolman_id"] != mapped_id:
        raise ValueError(f"slot {slot} spool mismatch: requested #{selection['spoolman_id']}, configured #{mapped_id}")
    def same_selection(key, configured_value, chosen):
        if key == "color" and filament_color.parse_hex(configured_value) and filament_color.parse_hex(chosen):
            # Both are hex: #FFF, FFFFFF and FAFAFA are one color, 067749 and 0C6283 are not.
            return filament_color.compare_colors(configured_value, chosen) == "same"
        return " ".join(configured_value.strip().casefold().split()) == " ".join(chosen.strip().casefold().split())
    for key in ("color", "material"):
        value = configured.get(key)
        if value is not None and (not isinstance(value, str) or not same_selection(key, value, selection[key])):
            raise ValueError(f"slot {slot} {key} does not match the configured selection")
    return selection


def save_filament_selection(job_path, selection):
    """Persist a confirmed selection only in the explicitly named job file."""
    path = Path(job_path)
    job = json.loads(path.read_text())
    if not isinstance(job, dict) or not job.get("model_id"):
        raise ValueError("not a model JOB.json")
    job["filament_selection"] = validate_filament_selection(selection, limit=position_limit(_job_adapter(job)))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(job, indent=2) + "\n")
    os.replace(tmp, path)
    return job["filament_selection"]


READINESS_MAX_AGE_SECONDS = 15


def _native():
    """True when the configured printer has the ZMod native-screen runtime (display_off, offsets, stock API)."""
    return printers.current().has_native_screen


def _gcode_tools(text):
    """Tool indexes a G-code selects with a bare Tn line."""
    return {int(m.group(1)) for line in text.splitlines()
            if (m := re.fullmatch(r"T(\d+)(?:\s*;.*)?", line.strip()))}


def _refuse_multicolor(text):
    """Non-multi-slot families refuse a G-code that selects any tool but T0, through the adapter."""
    adapter = printers.current()
    adapter.normalize_tool_mapping({str(t): t + 1 for t in sorted(_gcode_tools(text) | {0})}, {})
    return adapter


def telemetry_state(value):
    """Classify raw telemetry without coercing strings, numbers, or null to booleans."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def check_filament_readiness(state, status, selection=None, *, observed_at=None, now=None):
    """Fail closed unless explicit, fresh telemetry agrees with the job's AD5X selection."""
    problems = []
    now = time.time() if now is None else now
    if (not isinstance(now, (int, float)) or isinstance(now, bool) or
            not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool)):
        problems.append("telemetry freshness is unknown")
    elif (now - observed_at < 0 or now - observed_at > READINESS_MAX_AGE_SECONDS):
        problems.append("telemetry is stale or has an invalid timestamp")

    sensor = status.get("filament_switch_sensor head_switch_sensor") if isinstance(status, dict) else None
    if not isinstance(sensor, dict) or telemetry_state(sensor.get("enabled")) != "true":
        problems.append(f"head filament sensor enabled state is {telemetry_state(sensor.get('enabled') if isinstance(sensor, dict) else None)}")
    head = sensor.get("filament_detected") if isinstance(sensor, dict) else None
    print_stats = status.get('print_stats') if isinstance(status, dict) else None
    phase = print_stats.get('state') if isinstance(print_stats, dict) else None
    staged_idle = phase in ('standby', 'complete', 'cancelled')
    if telemetry_state(head) == "unknown" or (head is False and not staged_idle):
        if telemetry_state(head) == "unknown":
            problems.append(f"head filament sensor is unknown (value {head!r})")
        else:
            problems.append("head filament sensor reports no filament during a non-idle print phase (possible runout)")
    # An enabled, empty head is normal before IFS loading. Unknown execution
    # state and active/paused runout never receive the idle staging exemption.
    if isinstance(selection, dict) and "filament_selection" in selection:
        selection = selection.get("filament_selection")
    if isinstance(selection, dict):
        try:
            selection = validate_filament_selection(selection)
        except ValueError as exc:
            problems.append(str(exc))
            return problems
    color = status.get("zmod_color") if isinstance(status, dict) else None
    slots = color.get("slots") if isinstance(color, dict) else None
    if not isinstance(slots, list):
        problems.append("AD5X IFS slot inventory is unavailable")
        return problems
    if not isinstance(selection, dict):
        problems.append("job has no explicit IFS slot/color selection")
        return problems
    try:
        selection = validate_filament_selection(selection)
    except ValueError as exc:
        problems.append(str(exc))
        return problems
    slot_id = selection["slot"]
    def slot_id_of(item):
        raw = item.get("ID") if isinstance(item, dict) else None
        if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= FILAMENT_SLOTS:
            return raw
        if isinstance(raw, str) and raw.isdigit() and 1 <= int(raw) <= FILAMENT_SLOTS:
            return int(raw)
        return None

    malformed = [item for item in slots if slot_id_of(item) is None]
    if malformed:
        problems.append("AD5X IFS inventory contains malformed slot IDs")
    matches = [item for item in slots if slot_id_of(item) == selection["slot"]]
    if len(matches) > 1:
        problems.append(f"AD5X IFS inventory contains duplicate ID {slot_id}")
        return problems
    if not matches:
        problems.append(f"selected IFS slot {slot_id} is unavailable")
        return problems
    actual = matches[0]
    feeder = actual.get("hasFilament")
    feeder_state = telemetry_state(feeder)
    if feeder_state == "false":
        problems.append(
            f"selected IFS slot {slot_id} reports false (empty). Check the printer screen's IFS view for slot {slot_id}; "
            f"if it is empty, feed filament into slot {slot_id}'s inlet until the IFS grips it, then retry. "
            "The AD5X has no buffer switch to click")
    elif feeder_state != "true":
        problems.append(
            f"selected IFS slot {slot_id} presence is unknown (raw value {feeder!r}); "
            "this is a telemetry fault, not proof the slot is empty")
    for key, actual_key in (("material", "Material"), ("color", "Color")):
        wanted = selection.get(key)
        observed = actual.get(actual_key)
        if not isinstance(wanted, str) or not wanted.strip():
            problems.append(f"job selection is missing {key}")
        elif not isinstance(observed, str) or observed.casefold() != wanted.casefold():
            problems.append(f"selected IFS slot {slot_id} {key} does not match the job selection")
    return problems


def read_filament_telemetry(state):
    """Read and label filament telemetry without triggering any printer action."""
    objects = urllib.parse.quote("&".join(FILAMENT_SENSOR_OBJECTS), safe="&")
    try:
        response = request_json(f"{state['moonraker_url']}/printer/objects/query?{objects}")
        status = response.get("result", {}).get("status", {}) if isinstance(response, dict) else {}
    except Exception as exc:
        status = {}
        query_error = f"{type(exc).__name__}: {exc}"
    else:
        query_error = None
    if not isinstance(status, dict):
        status = {}
        query_error = query_error or "invalid Moonraker status payload"
    sensor = status.get("filament_switch_sensor head_switch_sensor")
    color = status.get("zmod_color")
    slots = color.get("slots") if isinstance(color, dict) else None
    raw_zmod_slots = [
        {"ID": item.get("ID"), "hasFilament": item.get("hasFilament")}
        for item in slots if isinstance(item, dict)
    ] if isinstance(slots, list) else "unknown"
    ifs = status.get("zmod_ifs")
    ports = ifs.get("Ports") if isinstance(ifs, dict) else None
    variables_obj = status.get("save_variables")
    variables = variables_obj.get("variables") if isinstance(variables_obj, dict) else None
    mode = variables.get("display_off") if isinstance(variables, dict) else None
    if type(mode) is not int or mode not in (0, 1):
        mode = None
    mode_name = "native" if mode == 0 else "screenless" if mode == 1 else "unknown"
    stock_detail_error = None
    stock_detail_used = False
    if mode == 0:
        try:
            native_slots = read_stock_filament_slots(state)
            # Native authenticated /detail is authoritative for occupancy.
            apply_stock_presence(status, native_slots)
            slots = color.get("slots") if isinstance(color, dict) else None
            stock_detail_used = True
        except Exception as exc:
            # Preserve raw ZMod telemetry, but never present its known-stale
            # native-mode occupancy flags as evidence of empty or loaded slots.
            stock_detail_error = f"{type(exc).__name__}: {exc}"
            if isinstance(slots, list):
                for item in slots:
                    if isinstance(item, dict):
                        item["hasFilament"] = "unknown"
                        item["presence_source"] = "conflicted: stock /detail unavailable; raw zmod_color preserved separately"
    return {
        "queried_at_epoch": time.time(),
        "source": "Moonraker printer/objects/query",
        "printer_mode": mode_name,
        "display_off": mode if mode in (0, 1) else "unknown",
        "stock_detail_authoritative": stock_detail_used,
        "stock_detail_error": stock_detail_error,
        "raw_zmod_color_presence": raw_zmod_slots,
        "freshness_note": "Query time is recorded; the endpoint does not provide per-field sample timestamps, so underlying sensor freshness is unknown.",
        "query_error": query_error,
        "configured_palette": [
            {"ID": item.get("ID"), "Material": item.get("Material"), "Color": item.get("Color")}
            for item in slots if isinstance(item, dict)
        ] if isinstance(slots, list) else "unknown",
        "feeder_slots": [
            {"ID": item.get("ID"), "hasFilament": telemetry_state(item.get("hasFilament"))}
            for item in slots if isinstance(item, dict)
        ] if isinstance(slots, list) else "unknown",
        "feeder_ports_zero_based": [telemetry_state(port) for port in ports]
        if isinstance(ports, list) else "unknown",
        "feeder_slot_mapping": "zmod_color slot IDs are 1-based; zmod_ifs Ports list indices are 0-based",
        "ifs_enabled": telemetry_state(ifs.get("ifs")) if isinstance(ifs, dict) else "unknown",
        "silk_state": ifs.get("Silk") if isinstance(ifs, dict) else "unknown",
        "toolhead": {
            "source_object": "filament_switch_sensor head_switch_sensor",
            "filament_detected": telemetry_state(sensor.get("filament_detected"))
            if isinstance(sensor, dict) else "unknown",
            "enabled": telemetry_state(sensor.get("enabled")) if isinstance(sensor, dict) else "unknown",
        },
    }


# Stock firmware /detail readers live with the ZMod code; these delegate through the printer adapter.
def read_stock_detail(state, *, post=None):
    """Read native status using profile credentials without logging them."""
    return printers.current().native.read_stock_detail(state, post=post)


def validate_native_start(detail, selection):
    """Require native ready and an explicit physical-slot route, not stale feeder state."""
    return printers.current().native.validate_native_start(detail, selection)


def read_stock_filament_slots_from_detail(detail):
    """Extract native slot presence from one already-fetched authenticated detail sample."""
    return printers.current().native.read_stock_filament_slots_from_detail(detail)


def read_stock_filament_slots(state, *, post=None):
    """Native presence, not the screen-mode hardcoded zmod_color flags."""
    return read_stock_filament_slots_from_detail(read_stock_detail(state, post=post))


def apply_stock_presence(status, stock):
    """Replace zmod_color hasFilament with stock presence; unknown stays unknown."""
    return printers.current().native.apply_stock_presence(status, stock)


def _validate_slot_selection(state, selection):
    """The selected IFS slot, not the globally active spool, owns a single-color job."""
    slot = state.get('filament_slots', {}).get(str(selection['slot']), {})
    mapping_matches = (slot.get('spoolman_id') and slot.get('spoolman_id') == selection['spoolman_id']
                       and str(slot.get('material', '')).casefold() == selection['material'].casefold())
    if mapping_matches and not slot.get('physical_color_verified'):
        # Only the color confirmation is missing: say so plainly and offer
        # the one-button fix instead of a retry that fails the same way.
        from .slice_refusal import unconfirmed_slot_color
        raise unconfirmed_slot_color(
            selection['slot'], slot,
            f"selected IFS slot {selection['slot']} is unverified or its material/spool mapping differs")
    if (not slot.get('physical_color_verified') or not slot.get('spoolman_id')
            or slot.get('spoolman_id') != selection['spoolman_id']
            or str(slot.get('material', '')).casefold() != selection['material'].casefold()):
        # A different spool or material is never waved through: plain reason, no one-tap fix.
        from .slice_refusal import slot_mismatch
        raise slot_mismatch(selection['slot'], slot, selection,
                            f"selected IFS slot {selection['slot']} is unverified or its material/spool mapping differs")
    return slot


def _sync_active_spool(st, slot):
    """Point Moonraker and the hardware record at the launching slot's spool.

    Runs only after every launch preflight has passed and before upload.
    Nozzle and bed temperatures are left alone; per-slot temperatures are
    Phase 2.

    Moonraker is a launch dependency: its active-spool read failing refuses
    the launch, and a readback that ANSWERS with a different spool refuses
    too (a live contradiction, nothing uploaded). Spoolman is not a launch
    dependency (#387): when Spoolman does not answer, or the Moonraker
    spool-pointer write itself fails, the launch proceeds on the slot's
    recorded data, the user gets the one-line notice, and the result
    carries `spoolman_pending` so the caller records the usage as pending
    in the job record for later reconciliation — it is never retried in a
    way that blocks the job. A live Spoolman answer that the spool is
    archived or a different material than the slot record still refuses:
    that is Spoolman data contradicting the operator's record, not an
    outage, and the refusal protects the physical gate (#235).
    """
    configured = st.get('filament_slots', {}).get(str(slot), {})
    sid = configured.get('spoolman_id')
    if not isinstance(sid, int) or isinstance(sid, bool):
        raise RuntimeError(f'IFS slot {slot} has no mapped Spoolman spool; nothing uploaded or started')
    try:
        current = _mr(st, '/server/spoolman/spool_id')['result'].get('spool_id')
    except Exception as exc:
        raise RuntimeError(f'Moonraker active spool read failed: {exc}; nothing uploaded or started') from exc
    if current == sid and st.get('spool', {}).get('spoolman_id') == sid:
        return {'spool_id': sid, 'changed': False}
    spool = spoolman_client.get_spool(sid, base_url=printer_state.spoolman_url(st))
    pending = spool is None
    if pending:
        spoolman_client.note_unavailable_once()
        filament = {}
    else:
        filament = spool.get('filament') or {}
        if spool.get('archived') or not printer_state.same_material(filament.get('material'), configured.get('material')):
            raise RuntimeError(f'Spoolman spool #{sid} is archived or its material differs from slot {slot}; nothing uploaded or started')
    try:
        _mr(st, '/server/spoolman/spool_id', data={'spool_id': sid})
        back = _mr(st, '/server/spoolman/spool_id')['result'].get('spool_id')
    except Exception as exc:
        # The pointer write is Spoolman accounting routed through Moonraker;
        # its failure must not refuse the launch (#387). Usage may charge
        # the previous spool or none until reconciliation.
        logger.warning('active spool sync failed for spool #%s: %s', sid, exc)
        spoolman_client.note_unavailable_once()
        pending = True
    else:
        if back != sid:
            raise RuntimeError(f'Moonraker active spool readback {back!r} != #{sid}; nothing uploaded or started')
    recorded = st.get('spool') or {}
    fresh = printer_state.load()
    fresh['spool'] = {'spoolman_id': sid,
                      'name': filament.get('name') or configured.get('name'),
                      'material': filament.get('material') or configured.get('material'),
                      'diameter_mm': filament.get('diameter', recorded.get('diameter_mm', 1.75)),
                      'density': filament.get('density', recorded.get('density', 1.24))}
    # temps_c belonged to the previous spool: mark it so slicing resolves this one's (#692).
    fresh['temperature_resolution'] = {'source': 'spool changed by launch sync; resolved per slice'}
    printer_state.save(fresh)
    st['spool'] = dict(fresh['spool'])
    print(f'ACTIVE SPOOL SYNCED: #{current} -> #{sid} (slot {slot})', flush=True)
    result = {'spool_id': sid, 'previous': current, 'changed': True}
    if pending:
        result['spoolman_pending'] = True
    return result


def _verify_readiness_adapter(adapter, state, selection=None):
    """Families without IFS: one Moonraker sample judged by the adapter's own readiness check."""
    try:
        objects = urllib.parse.quote("&".join(adapter.moonraker_objects()), safe="&")
        status = request_json(f"{state['moonraker_url']}/printer/objects/query?{objects}")["result"]["status"]
        problems = adapter.readiness_problems(state, status, selection)
        if problems:
            raise RuntimeError("; ".join(problems))
    except Exception as exc:
        raise RuntimeError(f"{adapter.label} readiness unavailable; print refused: {exc}") from exc


def _verify_filament_readiness(state, selection=None):
    """Read current mode, then obtain one freshness-bounded sensor/occupancy sample."""
    adapter = printers.current()
    if not adapter.multi_slot:
        return _verify_readiness_adapter(adapter, state, selection)
    try:
        selection = resolve_filament_selection(state, selection)
        _validate_slot_selection(state, selection)
    except ValueError as exc:
        raise RuntimeError(f"filament readiness refused: {exc}") from exc
    # Phase 1: the Moonraker active spool is an accounting pointer that follows
    # the job at launch (_sync_active_spool); it is not a physical feed fact.
    try:
        mode_data = _mr(state, "/printer/objects/query?save_variables")["result"]["status"]
        mode = (mode_data.get("save_variables") or {}).get("variables", {}).get("display_off")
        if mode not in (0, 1):
            raise RuntimeError(f"printer mode unknown: display_off={mode!r}")
        response = request_json(f"{state['moonraker_url']}/printer/objects/query?{urllib.parse.quote('&'.join(FILAMENT_SENSOR_OBJECTS), safe='&')}")
        status = response["result"]["status"]
        observed_at = time.time()
        if mode == 0:
            detail = read_stock_detail(state)
            native_at = time.time()
            native_phase = detail.get("status")
            moonraker_phase, moonraker_file, _ = _printer_phase(state)
            if native_phase == "printing" and moonraker_phase not in ("printing", "paused"):
                raise RuntimeError("native status='printing' conflicts with Moonraker standby; execution state unresolved")
            if native_phase in ("ready", "idle", "standby") and moonraker_phase in ("printing", "paused"):
                raise RuntimeError(f"Moonraker {moonraker_phase} conflicts with native status={native_phase!r}; execution state unresolved")
            apply_stock_presence(status, read_stock_filament_slots_from_detail(detail))
            observed_at = native_at
        problems = check_filament_readiness(state, status, selection, observed_at=observed_at)
        if problems:
            raise RuntimeError("; ".join(problems))
    except Exception as exc:
        raise RuntimeError(f"filament readiness unavailable; print refused: {exc}") from exc


def _with_gcode_temperatures(state, text):
    """state with temps_c set to what this G-code's own spool resolves to (printer_state.gcode_temperatures),
    so a slot job sliced at its spool's temperature is checked against that, not the printer-wide record."""
    from .orca_config import split_config_list
    from . import spoolman_client
    match = re.search(r'^; filament_type = (.+)$', text, re.M)
    try:
        materials = split_config_list(match.group(1).strip()) if match else []
    except ValueError:
        materials = []

    def fetch(spool_id):
        record = spoolman_client.get_spool(spool_id, base_url=printer_state.spoolman_url(state))
        return None if not record or record.get('archived') else record.get('filament')
    try:
        temps = printer_state.gcode_temperatures(state, materials, fetch, text)
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError(f"G-code temperature check failed: {exc}") from exc
    return {**state, 'temps_c': temps}


def verify_gcode_offline(gcode_path, state):
    """Check startup, checksum, and bed bounds without contacting the printer.

    The live sibling reads display mode from Moonraker. This one validates the
    artifact against the already loaded hardware record: MD5 when present,
    native heater prefix, nozzle, material, start and end, and motion bounds.
    """
    if printers.current().connection in ('offline', 'bambulab_lan', 'prusalink', 'elegoo_sdcp'):
        return _verify_offline(gcode_path, state)
    p = Path(gcode_path)
    raw = p.read_bytes()
    text = raw.decode(errors='ignore')
    errors = []
    if raw.startswith(b'; MD5:') and not zmod_md5_valid(p):
        errors.append('invalid ZMod MD5 checksum')
    state = _with_gcode_temperatures(state, text)
    from .nozzle import effective_nozzle_temperature
    nozzle_type = (state.get('nozzle') or {}).get('type') or settings.current().get('printer', {}).get('nozzle_type') or 'hardened_steel'
    try:
        hotend = effective_nozzle_temperature(nozzle_type, float(state['temps_c']['nozzle']))
    except Exception as exc:
        raise RuntimeError("offline G-code verification failed: invalid effective nozzle temperature: " + str(exc)) from exc
    if not native_heater_lines_valid(text, state['temps_c']['bed'], hotend):
        errors.append('startup heater prefix does not match the recorded bed and effective nozzle temperature')
    def config(key):
        match = re.search(rf'^; {re.escape(key)} = (.+)$', text, re.M)
        return match.group(1).strip() if match else None
    nozzle = config('nozzle_diameter')
    if nozzle is None or abs(float(nozzle) - state['nozzle']['diameter_mm']) > 1e-3:
        errors.append(f"nozzle {nozzle} != recorded {state['nozzle']['diameter_mm']}")
    hotend, bed = effective_nozzle_temperature(nozzle_type, float(state['temps_c']['nozzle'])), state['temps_c']['bed']
    start_match = re.search(r'^START_PRINT\s+([^;]*)', text, re.M)
    start_args = dict(re.findall(r'([A-Z_]+)=([+-]?(?:\d+(?:\.\d*)?|\.\d+))', start_match.group(1))) if start_match else {}
    start_matches = (start_match is not None and
                     math.isclose(float(start_args.get('BED_TEMP', 'nan')), float(bed), abs_tol=1e-3) and
                     math.isclose(float(start_args.get('EXTRUDER_TEMP', 'nan')), float(hotend), abs_tol=1e-3))
    if not start_matches:
        errors.append(f'missing START_PRINT matching bed {bed} and effective hotend {hotend}')
    if not re.search(r'^END_PRINT(?:\s|$)', text, re.M):
        errors.append('missing END_PRINT')
    bounds = cached_motion_bounds(raw, text)
    limits = state.get('axis_limits_mm', {'x': [0, BED_MM], 'y': [0, BED_MM], 'z': [0, state.get('max_print_height_mm', MAX_HEIGHT_MM)]})
    for axis in 'xyz':
        low, high = bounds[axis]
        limit_low, limit_high = map(float, limits[axis])
        if low < limit_low - 1e-3 or high > limit_high + 1e-3:
            errors.append(f'{axis.upper()} bounds {low:.3f}..{high:.3f} exceed {limit_low:.3f}..{limit_high:.3f}')
    if errors:
        raise RuntimeError('offline G-code verification failed: ' + '; '.join(errors))
    return {'bounds_mm': bounds, 'nozzle': nozzle, 'offline': True}


def _verify_offline(gcode_path, state):
    """A printer Print Handy cannot reach (bambulab.a1): the adapter's checks against the hardware record."""
    from .printer_state import _material_temperature_contract
    try:
        _material_temperature_contract(state)
    except RuntimeError as exc:
        raise RuntimeError(f'offline G-code verification failed: {exc}') from exc
    text = Path(gcode_path).read_text(errors='ignore')
    problems = printers.current().gcode_problems(text, _with_gcode_temperatures(state, text))
    if problems:
        raise RuntimeError('offline G-code verification failed: ' + '; '.join(problems))
    return {'offline': True, 'connection': printers.current().connection}


def _detail_tail(detail):
    """Demote raw machinery to a secondary Detail line (#467).

    User-facing pipeline exits lead with a plain sentence; the raw stderr
    tail / response body that used to *be* the message rides along after
    it, whitespace-collapsed and capped, so a terminal or log reader keeps
    the evidence without a chat reader having to start there.
    """
    text = " ".join(str(detail or "").split())[:300]
    return f"\nDetail: {text}" if text else ""


def _print_check_warnings(facts):
    """Surface non-blocking check warnings (Spoolman, #235) without refusing work.

    Warnings go to stderr, the channel #240 established for verify_gcode, so
    every caller of this helper reports each warning exactly once.
    """
    for warning in (facts or {}).get("warnings") or []:
        print(f"WARNING: {warning}", file=sys.stderr, flush=True)


def verify_gcode(gcode_path):
    """Check a G-code artifact against live nozzle, spool and temperature state.

    Spoolman is never a gate here (#235): printer_state.check reports its
    outcomes as warnings, and verification proceeds on the recorded state —
    the spool's recorded material/density and the recorded temperatures are
    the fallback when Spoolman is unreachable.
    """
    if printers.current().connection in ('offline', 'bambulab_lan', 'prusalink', 'elegoo_sdcp'):
        st = printer_state.load(); ok, probs, facts = printer_state.check(st)
        _print_check_warnings(facts)
        if not ok: raise RuntimeError("the printer check did not pass:\n  " + "\n  ".join(probs))
        return _verify_offline(gcode_path, st)
    st=printer_state.load(); ok, probs, facts=printer_state.check(st)
    _print_check_warnings(facts)
    if not ok: raise RuntimeError("the printer check did not pass:\n  " + "\n  ".join(probs))
    p=Path(gcode_path); raw=p.read_bytes(); text=raw.decode(errors="ignore")
    errors=[]
    # The installed native screen requires ZMod's documented first-line MD5.
    # Screenless files remain accepted without it, but any present checksum is validated.
    if raw.startswith(b"; MD5:") and not zmod_md5_valid(p):
        errors.append("invalid ZMod MD5 checksum")
    native = _native()
    mode = _mr(st, "/printer/objects/query?save_variables")["result"]["status"]["save_variables"]["variables"].get("display_off") if native else None
    nozzle_type = (st.get("nozzle") or {}).get("type") or settings.current().get("printer", {}).get("nozzle_type") or "hardened_steel"
    st = _with_gcode_temperatures(st, text)
    from .printer_state import _material_temperature_contract
    try:
        _material_temperature_contract(st)
    except RuntimeError as exc:
        errors.append(str(exc))
    from .nozzle import effective_nozzle_temperature
    effective_hotend = effective_nozzle_temperature(nozzle_type, st["temps_c"]["nozzle"])
    if not native:
        pass  # no native screen: no MD5 or startup-heater contract
    elif mode == 0:
        if not zmod_md5_valid(p):
            errors.append("native screen requires valid ZMod MD5 checksum")
        if not native_heater_lines_valid(text, st["temps_c"]["bed"], effective_hotend):
            errors.append("native screen requires immediate startup M190 Sbed / M104 Snozzle T0 consistent with START_PRINT; later heaters do not count")
    elif mode != 1:
        errors.append(f"unknown display_off mode {mode!r}; refusing hardware-dependent G-code validation")
    def config(key):
        m=re.search(rf'^; {re.escape(key)} = (.+)$', text, re.M)
        return m.group(1).strip() if m else None
    nozzle=config("nozzle_diameter")
    nozzle_value=None
    if nozzle is None:
        errors.append("missing embedded nozzle_diameter")
    else:
        try:
            nozzle_value=float(nozzle)
        except ValueError:
            errors.append(f"invalid nozzle_diameter {nozzle!r}")
        else:
            if abs(nozzle_value-st["nozzle"]["diameter_mm"])>1e-3:
                errors.append(f"G-code nozzle {nozzle} != live {st['nozzle']['diameter_mm']}")
    material=config("filament_type")
    if native:
        validate_materials = printers.current().native.validate_materials
        try:
            validate_materials(text, material, st)
        except RuntimeError as exc:
            errors.append(str(exc))
    else:
        _refuse_multicolor(text)
        from .orca_config import split_config_list
        try:
            # Orca quotes a material with a space: `"Silk PLA"` is the material Silk PLA.
            listed = split_config_list(material)
        except ValueError:
            listed = []
        if len(listed) == 1:
            material = listed[0]
        if not printer_state.same_material(material, st["spool"]["material"]):
            errors.append(f"G-code filament_type {material!r} != loaded {st['spool']['material']!r}")
    hotend,bed=effective_hotend,st["temps_c"]["bed"]
    def numeric_macro_match(pattern, bed_key, hotend_key):
        match = re.search(pattern, text, re.M)
        args = dict(re.findall(r'([A-Z_]+)=([+-]?(?:\d+(?:\.\d*)?|\.\d+))', match.group(1))) if match else {}
        try:
            return (match is not None and
                    math.isclose(float(args.get(bed_key, 'nan')), float(bed), abs_tol=1e-3) and
                    math.isclose(float(args.get(hotend_key, 'nan')), float(hotend), abs_tol=1e-3))
        except ValueError:
            return False
    octoprint = getattr(printers.current(), 'transport', None) == 'octoprint'
    if octoprint:
        for command, target in (('M190', bed), ('M109', hotend)):
            values = re.findall(rf'^{command}\s+S([\d.]+)', text, re.M)
            if not values or not math.isclose(float(values[0]), float(target), abs_tol=1e-3):
                errors.append(f'missing {command} matching target {target}')
        # Validate the slicer-owned shutdown, never append a guessed end sequence.
        if not all(re.search(rf'^{cmd}\s+S0(?:\s|$)', text, re.M) for cmd in ('M104', 'M140')):
            errors.append('Marlin slicer end G-code must turn off nozzle and bed (M104 S0, M140 S0)')
    elif not native:
        macro = printers.current().start_macro
        if not (numeric_macro_match(rf'^{re.escape(macro)}\s+([^;]*)', 'BED', 'HOTEND') or
                sovol_style_start_matches(text, bed, hotend)):
            errors.append(f"missing {macro} matching bed {bed} and effective hotend {hotend}"
                          " (accepted starts: the configured macro with BED/HOTEND arguments,"
                          " START_PRINT with BED_TEMP/EXTRUDER_TEMP arguments, or a bare"
                          " START_PRINT whose M190/M109 heater lines match those temperatures)")
    elif not (numeric_macro_match(r'^PRINT_START\s+([^;]*)', 'BED', 'HOTEND') or
              numeric_macro_match(r'^START_PRINT\s+([^;]*)', 'BED_TEMP', 'EXTRUDER_TEMP')):
        errors.append(f"missing START_PRINT/PRINT_START matching bed {bed} and effective hotend {hotend}")
    if not octoprint and not (re.search(r'^PRINT_END(?:\s|$)',text,re.M) or re.search(r'^END_PRINT(?:\s|$)',text,re.M)
            or (not native and re.search(rf'^{re.escape(printers.current().end_macro)}(?:\s|$)',text,re.M))):
        errors.append("missing END_PRINT/PRINT_END safe end sequence")
    try:
        bounds=cached_motion_bounds(raw, text)
    except RuntimeError as exc:
        errors.append(f"toolpath bounds unavailable: {exc}")
        bounds=None
    if bounds:
        limits=st.get("axis_limits_mm", {"x":[0,223],"y":[0,223],"z":[0,st.get("max_print_height_mm",250)]})
        for axis in "xyz":
            low,high=bounds[axis]
            limit_low,limit_high=map(float,limits[axis])
            if low<limit_low-1e-3 or high>limit_high+1e-3:
                errors.append(f"{axis.upper()} bounds {low:.3f}..{high:.3f} exceed {limit_low:.3f}..{limit_high:.3f}")
    first_layer_height=config("first_layer_height") or config("initial_layer_print_height")
    try:
        if first_layer_height is None or float(first_layer_height)<=0:
            errors.append(f"invalid first_layer_height {first_layer_height!r}")
    except ValueError:
        errors.append(f"invalid first_layer_height {first_layer_height!r}")
    # Lines narrower than 90% of the orifice are refused, first layer and body alike.
    if nozzle_value is not None:
        floor=0.9*nozzle_value
        layers=re.split(r'^;LAYER_CHANGE', text, flags=re.M)
        # Orca (notably with Arachne walls) writes ;WIDTH only when it changes, so a
        # layer can inherit its width from an earlier one. Carry the width in effect
        # across layers and count it once per structural feature (;TYPE) as well.
        carried = [None]
        def structural_widths(block):
            # Gap infill deliberately fills spaces narrower than the nozzle. Its
            # transition count is not a wall-flow measurement; never remove model
            # features merely to raise this metadata median.
            role = None
            widths = []
            for line in block.splitlines():
                if line.startswith(";TYPE:"):
                    role = line[6:].strip().casefold()
                    if carried[0] is not None and role not in ("gap infill", "gap fill"):
                        widths.append(carried[0])
                if line.startswith(";WIDTH:"):
                    carried[0] = float(line[7:])
                    if role not in ("gap infill", "gap fill"):
                        widths.append(carried[0])
            if not widths and carried[0] is not None and role not in ("gap infill", "gap fill") \
                    and re.search(r'^G[01] [^;\n]*E[0-9.]*[1-9]', block, re.M):
                # Extrudes with no new ;TYPE/;WIDTH line: the width in effect still applies.
                widths.append(carried[0])
            return widths
        structural_widths(layers[0] if layers else "")
        first=structural_widths(layers[1] if len(layers)>1 else "")
        body=structural_widths("".join(layers[2:6]))
        for name,ws in (("first layer",first),("layers 2-5",body)):
            if name == "layers 2-5" and len(layers) <= 2:
                # Single-layer test print (e.g. first-layer calibration pad) has no subsequent layers
                continue
            if not ws:
                errors.append(f"missing {name} WIDTH metadata")
                continue
            med=sorted(ws)[len(ws)//2]
            if med<floor: errors.append(f"{name} line width {med:.2f} mm < {floor:.2f} mm (90% of {nozzle} nozzle): starved extrusion, reslice")
    if errors: raise RuntimeError("the G-code did not pass its safety checks: " + "; ".join(errors))
    print(f"G-CODE OK: {p.name} | {printer_state.summary(st)}")
    return True


CONTEXT = Path(os.environ.get("PRINT_HANDY_CONTEXT_PATH", settings.state_dir()/"print_handy_context.json"))
RECORD_KEYS = ("layer_height","first_layer_height","perimeters","fill_density","fill_pattern","support_material","brim_width","nozzle_diameter","filament_type","temperature","bed_temperature")

def _job_for_gcode(g):
    """The archived JOB.json that produced G-code g.

    A split job keeps only plate 1 in JOB.json["gcode"]; plates 2..N live in
    derived_plates and multipart_plan.parts, so match those too (#48).
    """
    for jp in ARCHIVE.glob("*/JOB.json"):
        try: j=json.loads(jp.read_text())
        except (OSError, ValueError): continue
        if not isinstance(j, dict): continue
        paths=[j.get("gcode")]
        paths+=[r.get("gcode") for r in j.get("derived_plates") or [] if isinstance(r, dict)]
        paths+=[p.get("gcode_path") for p in (j.get("multipart_plan") or {}).get("parts") or [] if isinstance(p, dict)]
        if any(p and Path(p).resolve()==g for p in paths): return j
    return None


def copies_supported(job, multi_plate=None):
    """Whether slicing this job clones copies into its G-code (#37).

    Only the single plate path passes copies to Orca. Multi-plate, split and
    per-part jobs slice one copy per plate, so they must never be offered more.
    """
    if multi_plate is None:
        try: multi_plate = int((job.get('audit') or {}).get('plates') or 0) > 1
        except (TypeError, ValueError): multi_plate = False
    return not (multi_plate or job.get('full_size_split_plan') or job.get('full_size_rearrange_plan')
                or job.get('plate_mode') == 'separate'
                or (job.get('color_decisions') or {}).get('mode') == 'per_part')


def record_start(gcode_path, moonraker_name=None, started_at=None):
    """Write the JSON start record plus the completion sidecar for a started print."""
    print_records = _external.load("print_records")
    st=printer_state.load(); g=Path(gcode_path).expanduser().resolve(); text=g.read_text(errors="ignore")
    def cfg(key):
        m=re.search(rf'^; {re.escape(key)} = (.+)$', text, re.M); return m.group(1).strip() if m else None
    job=_job_for_gcode(g)
    key=moonraker_name or g.name
    src=(job or {}).get("selected_source") or {}
    ctx={"job_key":key,"title":(job or {}).get("title") or g.stem,"source_url":(job or {}).get("url"),
         "source_platform":(job or {}).get("site"),"started_at":started_at or print_records.utc_now(),
         "hardware":st.get("printer", "Klipper") + ", "+printer_state.summary(st),
         "printer_profile":"Klipper base ini, resliced locally (site settings not imported)",
         "estimated_time":cfg("estimated printing time (normal mode)") or _bambu_total_estimated_time(text),"estimated_filament_g":cfg("total filament used [g]") or _filament_total_g(text),
         "filament_diameter_mm":st["spool"]["diameter_mm"],"filament_density":st["spool"]["density"],
         "slicer_settings":{**{k:cfg(k) for k in RECORD_KEYS if cfg(k) is not None},
                            "source_profile":src.get("title"),"source_file":(job or {}).get("downloaded_file"),"local_gcode":str(g)},
         "active_spool":{"id":st["spool"]["spoolman_id"],"material":st["spool"]["material"],"color":st["spool"]["name"]}}
    plan = (job or {}).get('multipart_plan') or {}
    for part in plan.get('parts') or []:
        if part.get('gcode_path') and Path(part['gcode_path']).resolve() == g:
            # Split plates advance the same plan that /print-next reads.
            ctx.update(multipart_plan_id=plan['plan_id'], part_id=part['id'], required_parts=plan['parts'])
            break
    mapped = None
    current = printers.current()
    if current.multi_slot:
        # Only a native adapter has an embedded mapping to lift; a multi_slot
        # adapter without the native contract (Bambu AMS, #671 — the stock
        # AD5X never had one either) records the plain start, as it did while
        # it was classified single.
        native = current.native_or_none
        if native is not None:
            mapped = native.embedded_mapping(text, st)
    if mapped is not None:
        ctx['tool_to_physical_slot'] = mapped
        ctx['active_spool']['accounting_only_not_job_color'] = True
        ctx['slicer_settings']['mapped_materials'] = {
            tool: st['filament_slots'][str(slot)] for tool, slot in mapped.items()}
        ctx['printer_profile'] = 'Reviewed Orca AD5X native multicolor'
    if (job or {}).get('delivery_format'):
        ctx['delivery_format'] = job['delivery_format']
    ctx["record_path"]=print_records.write_start_record(ctx)
    record_path=ctx["record_path"]
    try: side=json.loads(CONTEXT.read_text())
    except (OSError, ValueError): side={}
    if not isinstance(side.get("jobs"),dict): side={"jobs":{}}
    side["jobs"][key]={**side["jobs"].get(key,{}),**ctx}
    tmp=CONTEXT.with_suffix(".tmp"); tmp.write_text(json.dumps(side,indent=2)+"\n"); os.replace(tmp,CONTEXT)
    print(f"PRINT RECORD: {ctx['record_path']}")
    return record_path


# Start announcement ownership. print_now claims the file before starting it so the
# Moonraker listener skips its generic "Print started" post for that same job.
# The family print paths claim the same way (#460): a claimed start belongs to
# the print path and its launched-print monitor; the family listener domains
# post starts only for prints nobody claimed (started outside Print Handy).
START_CLAIMS = Path(os.environ.get("PRINT_HANDY_START_CLAIMS", settings.state_dir()/"print_handy_start_claims.json"))
START_CLAIM_TTL_S = settings.current()["listener"]["start_claim_ttl_s"]


def _write_claims(claims):
    tmp = START_CLAIMS.with_suffix(".tmp"); tmp.write_text(json.dumps(claims)); os.replace(tmp, START_CLAIMS)


def _read_claims():
    try: claims = json.loads(START_CLAIMS.read_text())
    except (OSError, ValueError): return {}
    return claims if isinstance(claims, dict) else {}


def claim_start_announcement(remote, now=None):
    now = time.time() if now is None else now
    claims = {k: v for k, v in _read_claims().items()
              if isinstance(v, (int, float)) and now - v < START_CLAIM_TTL_S}
    claims[remote] = now
    _write_claims(claims)


def release_start_announcement(remote):
    claims = _read_claims()
    if claims.pop(remote, None) is not None:
        try: _write_claims(claims)
        except OSError: pass


def sovol_style_start_matches(text, bed, hotend):
    """Whether a START_PRINT-led job (Sovol stock style, issue #612) proves its temperatures.

    Sovol's own profiles lead with START_PRINT, not the PRINT_START BED/HOTEND
    form Print Handy's Klipper templates emit. Two shapes prove the job's
    temperatures, and only these:

    - START_PRINT carrying BED_TEMP=<bed> EXTRUDER_TEMP=<hotend> arguments, or
    - a bare START_PRINT whose first M190 (bed wait) and first M109 (nozzle
      wait) lines carry the temperatures — the shape the Sovol Zero profiles
      emit, where the macro takes no temperature arguments at all.

    A START_PRINT that carries temperature arguments which disagree never
    passes on its heater lines: the macro would run with its arguments, so
    the arguments are the contract the file states.
    """
    m = re.search(r'^START_PRINT\b\s*([^;]*)', text, re.M)
    if not m:
        return False
    args = dict(re.findall(r'([A-Z_]+)=([+-]?(?:\d+(?:\.\d*)?|\.\d+))', m.group(1)))
    if 'BED_TEMP' in args or 'EXTRUDER_TEMP' in args:
        try:
            return (math.isclose(float(args.get('BED_TEMP', 'nan')), float(bed), abs_tol=1e-3) and
                    math.isclose(float(args.get('EXTRUDER_TEMP', 'nan')), float(hotend), abs_tol=1e-3))
        except ValueError:
            return False

    def first_temp(command):
        match = re.search(rf'^{command}\s+S([+-]?(?:\d+(?:\.\d*)?|\.\d+))', text, re.M)
        return float(match.group(1)) if match else None

    bed_line, hotend_line = first_temp('M190'), first_temp('M109')
    return (bed_line is not None and hotend_line is not None and
            math.isclose(bed_line, float(bed), abs_tol=1e-3) and
            math.isclose(hotend_line, float(hotend), abs_tol=1e-3))


def start_print_settings(text):
    """Read the START_PRINT call the printer will actually execute."""
    m = re.search(r'^(?:START_PRINT|PRINT_START)\b(.*)$', text, re.M)
    if not m: return {"line": None, "args": {}}
    return {"line": m.group(0).strip(), "args": dict(re.findall(r'(\w+)=(\S+)', m.group(1)))}


def validate_saved_mesh_start(text, mesh):
    """Both modes must consume the reviewed mesh without new offset/calibration."""
    args = start_print_settings(text)['args']
    expected = mesh.get('profile')
    if (not expected or args.get('MESH') != expected or 'Z_OFFSET' in args or
            args.get('FORCE_LEVELING', 'false').lower() not in ('false', '0') or
            args.get('FORCE_KAMP', 'false').lower() not in ('false', '0')):
        raise RuntimeError(f'saved-mesh flow requires START_PRINT MESH={expected}, no explicit Z_OFFSET and no forced leveling'
                           f' (this file has MESH={args.get("MESH")}; it was sliced for a different screen mode'
                           ' or calibration, so prepare it again)')


Z_OFFSET_NOTICE = ("Z offset: no explicit START_PRINT Z_OFFSET; effective offset depends on the "
                   "installed macros and screen mode. Historical first-layer cause is not established "
                   "by this G-code. Do not inject or change an offset on this evidence alone.")


def z_offset_notice(text):
    """None when START_PRINT passes an explicit Z_OFFSET; otherwise the truthful notice.
    Informational only: with display_off=0 ZMod applies the native screen zProbeOffset, which is correct."""
    return None if "Z_OFFSET" in start_print_settings(text)["args"] else Z_OFFSET_NOTICE


# The 2026-10-03 "too high" prints logged `Printing with bed mesh: ` (blank). The good one loaded `auto`.
MESH_PROFILE = "auto"


def screen_z_offset(st):
    """zProbeOffset from the stock screen's Adventurer5M.json (what ZMod applies with display_off=0). Read-only.

    The captured schema stores it at leftExtruderOffset.zProbeOffset. A top-level or level.zProbeOffset
    is accepted only when the nested field is absent. Non-finite values fail closed as unreadable.
    """
    if not _native(): return None
    try:
        d=_mr(st,"/server/files/config/Adventurer5M.json",timeout=8)
    except Exception: return None
    def numeric(v):
        return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(float(v))
    nested=((d or {}).get("leftExtruderOffset") or {}).get("zProbeOffset") if isinstance(d,dict) else None
    if "leftExtruderOffset" in (d or {}) and isinstance(d.get("leftExtruderOffset"), dict) and "zProbeOffset" in d["leftExtruderOffset"]:
        return round(float(nested),3) if numeric(nested) else None
    def find(o):
        if isinstance(o,dict):
            v=o.get("zProbeOffset")
            if numeric(v): return v
            for x in o.values():
                r=find(x)
                if r is not None: return r
        return None
    v=find(d)
    return round(float(v),3) if numeric(v) else None


def mesh_readiness(st):
    """Read-only bed mesh check before upload. Returns dict; problem is None when the saved mesh is usable."""
    info: dict = {"profile":None,"saved_at":None,"print_leveling":None,"problem":None,"stale":None}
    if not _native():
        # The printer owns its mesh and offset; there is no saved-mesh contract to enforce.
        info.update(display_off=None, z_offset_mm=None, offset_source=None)
        return info
    try:
        q=_mr(st,"/printer/objects/query?bed_mesh=profiles&save_variables",timeout=8)["result"]["status"]
    except Exception as e:
        info["problem"]=f"could not read the bed mesh from Klipper ({type(e).__name__})"; return info
    profiles=(q.get("bed_mesh") or {}).get("profiles") or {}
    info["print_leveling"]=((q.get("save_variables") or {}).get("variables") or {}).get("print_leveling")
    variables=((q.get("save_variables") or {}).get("variables") or {})
    info["display_off"]=variables.get("display_off")
    saved_z=(variables.get("gcode_offsets") or {}).get("z")
    saved_numeric=not isinstance(saved_z, bool) and isinstance(saved_z, (int, float)) and math.isfinite(saved_z)
    # ZMod stores two offsets. display_off=1 (screenless) loads the numeric
    # save_variables gcode_offsets.z when LOAD_ZOFFSET=1. display_off=0 leaves
    # that value unused and applies the native screen zProbeOffset instead.
    # Neither path has a required magnitude. The two values are never added.
    if info["display_off"]==0:
        native=screen_z_offset(st)
        info["z_offset_mm"]=native
        info["saved_gcode_offset_mm"]=saved_z if saved_numeric else None
        info["offset_source"]="native screen zProbeOffset"
        if not isinstance(native, (int, float)) or isinstance(native, bool):
            info["problem"]="native screen is the offset owner but zProbeOffset is unreadable. Reconcile the screen calibration before starting; do not substitute the screenless saved offset"
            return info
    elif info["display_off"]==1:
        info["z_offset_mm"]=saved_z if saved_numeric else None
        info["offset_source"]="saved gcode offset"
        if variables.get("load_zoffset")!=1 or not saved_numeric:
            info["problem"]="screenless offset is not authoritative; expected LOAD_ZOFFSET=1 and a numeric saved gcode_offsets.z. The native screen offset is a separate store and is not used in this mode"
            return info
    else:
        info["z_offset_mm"]=None
        info["offset_source"]=None
        info["problem"]="display_off is neither 0 nor 1, so the offset owner is unknown. Reconcile before starting"
        return info
    if info["display_off"]==1 and variables.get("mesh_test", 1) not in (0, 1):
        info["problem"]="screenless saved-mesh flow permits MESH_TEST=0 or 1 only; modes 2/4 can rebuild and modes 3/4 auto-tune Z offset. Reconcile before starting"
        return info
    profile = MESH_PROFILE
    if info['display_off'] == 0:
        profile = (st.get('calibration_baseline') or {}).get('native_mesh', MESH_PROFILE)
    if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9_]+', profile):
        info['problem'] = 'invalid calibration baseline mesh'
        return info
    if profile not in profiles:
        info["problem"]=f"no saved '{profile}' bed mesh. Reconcile the selected calibration baseline before using the saved-mesh print flow"
        return info
    if info["print_leveling"] in (1,True,"1"):
        info["problem"]="PRINT_LEVELING=1 selects per-print rebuilding in screenless ZMod. This saved-mesh workflow requires PRINT_LEVELING=0; reconcile the chosen strategy before starting"
        return info
    info["profile"]=profile
    try:
        files=_mr(st,"/server/files/list?root=config",timeout=8)["result"]
        m=[f.get("modified") for f in files if f.get("path")=="printer.cfg"]
        if m and isinstance(m[0],(int,float)): info["saved_at"]=m[0]
    except Exception: pass
    try:
        inst=(st.get("nozzle") or {}).get("installed")
        if inst and info["saved_at"]:
            from datetime import datetime
            info["stale"]=datetime.fromisoformat(inst).timestamp() > info["saved_at"]
    except (ValueError, TypeError, AttributeError): pass
    return info


def mesh_line(mesh):
    if not isinstance(mesh,dict) or not mesh.get("profile"): return "Bed mesh: unknown"
    when="unknown"
    if mesh.get("saved_at"):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        when=datetime.fromtimestamp(mesh["saved_at"],ZoneInfo(TIMEZONE)).strftime("%-I:%M %p %a %b %-d")
    line=f"Bed mesh: {mesh['profile']} (config saved {when})"
    if mesh.get("stale"): line+=". Nozzle changed after this mesh; rerun full bed leveling"
    return line


def _assign_print_number(job_path):
    """Permanent print number from the shared /print-again registry; None on any failure."""
    if job_path is None:
        return None
    try:
        print_control = _external.load("print_control")
        ARCHIVE, assign_print_numbers = print_control.ARCHIVE, print_control.assign_print_numbers
        key = Path(job_path).resolve()
        if not key.is_relative_to(ARCHIVE.resolve()):
            return None  # scratch and test jobs are not prints
        return assign_print_numbers({key: time.time()}).get(str(key))
    except Exception as e:
        print(f"WARNING: print number not assigned: {e}")
        return None


def _maintenance_detail_line():
    try:
        maintenance = _external.load("maintenance")
        _call = _external.load("print_control")._call
        runs = maintenance.load().get("runs", [])
        hours = None
        try:
            data = _call("status")
            hours = data.get("print_hours") if isinstance(data, dict) else None
        except Exception: pass
        full = next((r for r in reversed(runs) if r.get("kind") == "full"), None)
        if full is None: return "- Maintenance: history unavailable"
        try:
            from datetime import datetime, timezone
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(full["at"])).days
        except Exception: age = "unknown"
        delta = "print hours unknown" if hours is None else f"{max(0,float(hours)-float(full.get('hours_total',0))):.1f} h"
        note = " (history totals unavailable)" if hours is None else ""
        return f"- Maintenance: last full clean {age} days / {delta} ago.{note}"
    except Exception: return "- Maintenance: history unavailable"


def job_details(out, *, hardware=None, selection=None, settings=None, record_path=None, z_notice=None, live=None,
                screen_z=None, mesh=None, printer=None, nozzle_mm=None, calibration=True):
    """Compact public summary; accounting, telemetry and paths stay in structured records."""
    def u(v): return v if v not in (None, "") else "unknown"
    args = (settings or {}).get("args") or {}
    bed = args.get("BED_TEMP") or args.get("BED"); hot = args.get("EXTRUDER_TEMP") or args.get("HOTEND")
    chan = (f"slot {selection.get('slot')}: {u(selection.get('color'))} {u(selection.get('material'))}"
            if isinstance(selection, dict) and selection.get("slot") is not None else None)
    grams = out.get("filament_g")
    # Historical records may include a timezone label; public details always assume New York.
    finish = re.sub(r"\s+(?:ET|EDT|EST)\b", "", str(u(out.get("finish_et"))))
    owner = (mesh or {}).get("offset_source") if isinstance(mesh, dict) else None
    source = owner or ("persisted" if isinstance(mesh, dict) else "screen")
    z = f"{screen_z:.3f} mm ({source})" if isinstance(screen_z, (int, float)) and not isinstance(screen_z, bool) else "unknown"
    return "\n".join([
        "**Job Details:**",
        *([f"- Print #{out['print_number']}"] if out.get("print_number") else []),
        f"- Model: {u(out.get('title'))}",
        f"- Estimated time: {u(out.get('print_time'))} printing + ~5 min warmup; finish {finish}",
        f"- Filament: {f'{grams} g' if grams is not None else 'unknown'}; {u(chan)}",
        f"- Printer: {u(printer)}",
        f"- Stock nozzle: {u(nozzle_mm)} mm; targets nozzle {u(hot)} C, bed {u(bed)} C",
        *([f"- Temperature material: {u((hardware or {}).get('spool', {}).get('material'))}; source: {u((hardware or {}).get('temperature_resolution', {}).get('source'))}"] if hardware else []),
        *([f"- Calibration: Z offset {z}; {mesh_line(mesh)}"] if calibration else []),
        _maintenance_detail_line(),
    ])


def get_author_settings(src_path):
    """Extract author brim and support configurations from a 3MF file."""
    import zipfile
    p = Path(src_path)
    res = {}
    if not p.is_file() or p.suffix.lower() != ".3mf":
        return res
    try:
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if n.endswith("project_settings.config"):
                    data = json.loads(z.read(n).decode("utf-8", errors="ignore"))
                    # Brim settings
                    bt = data.get("brim_type", "")
                    bw = data.get("brim_width")
                    if bt == "no_brim":
                        res["brim_width"] = "0"
                    elif bw is not None:
                        res["brim_width"] = str(bw)
                    # Support settings
                    es = data.get("enable_support")
                    if es is not None:
                        val = int(es) if str(es).isdigit() else (1 if es else 0)
                        res["support_material"] = str(val)
                    st = data.get("support_type")
                    if st and "tree" in str(st).lower():
                        res["support_material_style"] = "organic"
                    elif st:
                        # Bambu normal(auto)/normal(manual) is grid style; never leave the template's organic in place.
                        res["support_material_style"] = "grid"
                    break
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"unable to read author settings from {p}: {exc}") from exc
    return res

def decisions_digest(decisions):
    """Canonical geometry-to-filament choice identity, independent of dict order."""
    return hashlib.sha256(json.dumps(decisions, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def set_color_decisions(job, decisions):
    if decisions_digest(job.get('color_decisions')) != decisions_digest(decisions):
        if job.get('gcode'):
            job.setdefault('artifact_history', []).append({key: job[key] for key in
                ('gcode', 'gcode_verified', 'provenance', 'artifact_binding', 'sliced_at', 'slice_config',
                 'slice_recipe') if key in job})
        for key in ('gcode', 'provenance', 'artifact_binding', 'sliced_at', 'slice_config', 'slice_recipe'):
            job.pop(key, None)
        job['gcode_verified'] = False
    job['color_decisions'] = decisions
    if decisions['mode'] == 'single_color':
        job['filament_selection'] = decisions['selection']
    else:
        job.pop('filament_selection', None)


UNIT_QUESTION_MAX_MM = 10.0
INCH_TO_MM = 25.4


def normalize_units(value):
    """Canonical unit name for a user answer: 'mm' or 'inch'."""
    text = str(value or '').strip().lower()
    if text in ('mm', 'millimeter', 'millimeters', 'millimetre', 'millimetres'):
        return 'mm'
    if text in ('inch', 'inches', 'in'):
        return 'inch'
    raise ValueError('units must be mm or inch')


def _audit_dims(job):
    """Authored extents from the job audit, or None when they were not measured."""
    ad = job.get('audit') or {}
    try:
        dims = tuple(float(ad[key]) for key in ('size_x', 'size_y', 'size_z'))
    except (KeyError, TypeError, ValueError):
        return None
    return dims if all(value > 0 for value in dims) else None


def _is_generated_mm(job):
    """True when Print Handy itself generated the source, so its units are known mm."""
    provenance = job.get('unit_provenance')
    if isinstance(provenance, dict) and provenance.get('source') == 'generated' and provenance.get('units') == 'mm':
        return True
    return job.get('generated') is True or job.get('generated_units') == 'mm'


def make_unit_decision(units, source_sha256, dims):
    """A user unit confirmation bound to the exact source bytes it was given for."""
    return {'source_sha256': source_sha256, 'units': normalize_units(units),
            'original_mm': [float(value) for value in dims], 'source': 'user_confirmation'}


def set_unit_decision(job, decision):
    """Record a unit confirmation; a changed answer retires any artifact sliced under the old one."""
    if decisions_digest(job.get('unit_decision')) != decisions_digest(decision):
        if job.get('gcode'):
            job.setdefault('artifact_history', []).append({key: job[key] for key in
                ('gcode', 'gcode_verified', 'provenance', 'artifact_binding', 'sliced_at', 'slice_config',
                 'slice_recipe') if key in job})
        for key in ('gcode', 'provenance', 'artifact_binding', 'sliced_at', 'slice_config', 'slice_recipe'):
            job.pop(key, None)
        job['gcode_verified'] = False
    job['unit_decision'] = decision
    job['unit_provenance'] = {'units': decision['units'], 'source': 'user_confirmation',
                              'original_mm': decision['original_mm'],
                              'factor': INCH_TO_MM if decision['units'] == 'inch' else 1.0}


def unit_question(job):
    """The inch-vs-mm question for a small STL job, or None when its units are already settled.

    STL files do not record units. A largest authored dimension under 10 mm is
    ambiguous — a genuine millimeter pin and an inch-authored part look alike —
    so intake must ask instead of assuming inches. Units are settled by an
    explicit scale (including 1.0), by a source-bound user confirmation, or
    when Print Handy generated the part itself in millimeters.
    """
    src = job.get('downloaded_file')
    if not src or Path(str(src)).suffix.lower() != '.stl':
        return None
    dims = _audit_dims(job)
    if dims is None or max(dims) >= UNIT_QUESTION_MAX_MM:
        return None
    if job.get('scale') is not None:
        return None
    if _is_generated_mm(job):
        return None
    decision = job.get('unit_decision')
    if isinstance(decision, dict):
        try:
            normalize_units(decision.get('units'))
        except ValueError:
            pass
        else:
            known_sha = ((job.get('color_manifest') or {}).get('source_sha256')
                         or (job.get('audit') or {}).get('sha256'))
            if not decision.get('source_sha256') or not known_sha or decision['source_sha256'] == known_sha:
                return None
    return _unit_packet(dims)


def _unit_packet(dims):
    """The inch-vs-mm question packet for authored extents ``dims`` (mm as written)."""
    inch_dims = tuple(round(value * INCH_TO_MM, 6) for value in dims)
    measured = ' x '.join(f'{value:g}' for value in dims)
    as_inch = ' x '.join(f'{value:g}' for value in inch_dims)
    return {'status': 'UNIT_CONFIRMATION_REQUIRED',
            'original_mm': [float(value) for value in dims], 'inch_mm': list(inch_dims),
            'question': (f'STL files do not record units. This model measures {measured} mm as written: '
                         f'is that millimeters (prints at {measured} mm) or inches (prints at {as_inch} mm)?'),
            'choices': [f'Millimeters — print at {measured} mm', f'Inches — print at {as_inch} mm'],
            'resume': 'slice JOB.json --approved --units mm|inch (or unit-select JOB.json --units mm|inch)'}



def parts_unit_question(job, paths):
    """The inch-vs-mm question for the STL files of a multi-file job, or None when units are settled.

    A combined job is a 3MF (millimeters), so the single-STL check never sees
    its parts. Same rule on the parts instead: when the largest STL part is
    under UNIT_QUESTION_MAX_MM the set is ambiguous, and one answer covers all
    the STL files (3MF parts carry their own units).
    """
    if job.get('scale') is not None or _is_generated_mm(job):
        return None
    from .geometry_layout import load_parts
    from .model_parts import _bounds
    largest = None
    for path in paths:
        if Path(str(path)).suffix.lower() != '.stl':
            continue
        for part in load_parts(Path(path))[0]:
            lo, hi = _bounds(part['triangles'])
            dims = tuple(top - bottom for bottom, top in zip(lo, hi))
            if largest is None or max(dims) > max(largest):
                largest = dims
    if largest is None or not all(value > 0 for value in largest) or max(largest) >= UNIT_QUESTION_MAX_MM:
        return None
    return _unit_packet(largest)


def intake_unit_confirmation(job_path, units=None, interactive=False):
    """Normal intake gate: ask inch vs mm for a small STL and persist the answer on the job.

    Noninteractive callers receive a structured question packet and resume the
    same JOB via slice --units; no JSON file authoring is required. Settled
    jobs record their unit provenance and pass through.
    """
    path = Path(job_path)
    job = json.loads(path.read_text())
    question = unit_question(job)
    if question is None:
        dims = _audit_dims(job)
        if dims is not None and Path(str(job.get('downloaded_file') or '')).suffix.lower() == '.stl' \
                and not isinstance(job.get('unit_provenance'), dict):
            provenance = None
            if job.get('scale') is not None:
                provenance = {'units': None, 'source': 'explicit_scale',
                              'original_mm': [float(value) for value in dims], 'factor': float(job['scale'])}
            elif _is_generated_mm(job):
                provenance = {'units': 'mm', 'source': 'generated',
                              'original_mm': [float(value) for value in dims], 'factor': 1.0}
            elif max(dims) >= UNIT_QUESTION_MAX_MM:
                provenance = {'units': 'mm', 'source': 'authored_size',
                              'original_mm': [float(value) for value in dims], 'factor': 1.0}
            if provenance is not None:
                job['unit_provenance'] = provenance
                write_job(path.parent, job)
        return True
    question['job_json'] = str(path)
    if units is None:
        print(json.dumps(question, indent=2))
        if not interactive:
            return False
        answer = input('Units (mm or inch; blank stops): ').strip()
        if not answer:
            return False
        units = answer
    decision = make_unit_decision(units, file_sha256(job['downloaded_file']), question['original_mm'])
    set_unit_decision(job, decision)
    write_job(path.parent, job)
    return True


def require_current_artifact(job, gcode):
    """Offline fail-closed guard before telemetry, approval consumption or upload."""
    if not isinstance(job, dict):
        return
    binding = job.get('artifact_binding')
    if binding is None and not job.get('color_decisions'):
        return  # Preserve legacy jobs without source-color decisions.
    if not binding or job.get('gcode_verified') is not True:
        raise RuntimeError('stale color artifact: reslice current JOB before launch')
    from .slice_recipe import RECIPE_VERSION, require_recorded
    recipe_sha = require_recorded(job, binding)
    stored_hash = binding.get('gcode_sha256')
    presented = Path(gcode).resolve()
    if isinstance(stored_hash, dict):
        plates = job.get('derived_plates') or []
        normalized_hashes = {str(slot): digest for slot, digest in stored_hash.items()}
        normalized_slots = set()
        for row in plates:
            slot = row.get('slot')
            def _valid_slot(s):
                return not isinstance(s, bool) and (isinstance(s, int) or isinstance(s, str) and s.isdigit())
            # Painted multicolor plates carry every IFS slot they use.
            painted_ok = (row.get('binding_key') and isinstance(slot, list) and slot
                          and all(_valid_slot(s) for s in slot))
            if not painted_ok and not _valid_slot(slot):
                raise RuntimeError('stale color artifact: malformed physical IFS slot binding')
            slot_key = str(row.get('binding_key') or slot)
            if slot_key in normalized_slots:
                raise RuntimeError('stale color artifact: duplicate physical IFS slot binding')
            normalized_slots.add(slot_key)
        if normalized_slots != set(normalized_hashes):
            raise RuntimeError('stale color artifact: derived plates do not match the binding')
        allowed = set()
        for row in plates:
            plate = Path(row['gcode']).resolve()
            allowed.add(plate)
            if file_sha256(plate) != normalized_hashes[str(row.get('binding_key') or row.get('slot'))]:
                raise RuntimeError('stale color artifact: source, decisions or G-code changed; reslice before launch')
        if presented not in allowed or Path(job.get('gcode', '')).resolve() not in allowed:
            raise RuntimeError('stale color artifact: source, decisions or G-code changed; reslice before launch')
        expected_hash = stored_hash
    else:
        expected_hash = file_sha256(gcode)
        if Path(job.get('gcode', '')).resolve() != presented:
            raise RuntimeError('stale color artifact: source, decisions or G-code changed; reslice before launch')
    expected = {'source_sha256': file_sha256(job['downloaded_file']),
                'decisions_sha256': decisions_digest(job.get('color_decisions')),
                'gcode_sha256': expected_hash, 'recipe_version': RECIPE_VERSION, 'recipe_sha256': recipe_sha}
    if binding != expected:
        raise RuntimeError('stale color artifact: source, decisions or G-code changed; reslice before launch')


def _recipe_binding(job):
    """Recipe fields every artifact_binding carries; slice_job records the recipe before slicing."""
    return {'recipe_version': job['slice_recipe']['version'], 'recipe_sha256': job['slice_recipe']['sha256']}


def _recipe_changed(job, latest):
    """True when JOB.json, the hardware record, profiles or slicer now give another recipe than the one sliced."""
    from .slice_recipe import recipe_sha256, slice_recipe
    sliced = job['slice_recipe']['recipe']
    current = slice_recipe(latest, sliced['source_sha256'], printer_state.load(), **sliced['overrides'])
    return recipe_sha256(current) != job['slice_recipe']['sha256']


def stamp_job_printer(job, printer_id=None):
    """Record the creating printer's roster id on a new job (#334, design §2.2).

    A job learns its printer at creation and never re-derives it: every later
    path (state, lease, approval, adapter) keys off JOB.json's printer_id.
    Jobs written before the upgrade carry no id and belong to the configured
    single printer — see _job_printer_id.

    An explicit ``printer_id`` — the printer the intake channel routes to
    (#335) — wins over the settings default when the job does not name one
    yet; an unknown explicit id raises, naming it, rather than silently
    stamping a different printer.
    """
    if isinstance(job, dict) and not job.get("printer_id"):
        if printer_id:
            job["printer_id"] = settings.by_id(str(printer_id))["id"]
        else:
            try:
                job["printer_id"] = settings.default_printer_id()
            except Exception:
                pass
    return job


def _job_printer_id(job):
    """The roster printer id a job belongs to: its recorded printer_id, else the default printer."""
    if isinstance(job, dict) and job.get("printer_id"):
        return job["printer_id"]
    return settings.default_printer_id()


def _job_adapter(job):
    """Adapter for the printer a job belongs to.

    With a recorded printer_id (#334) the adapter is that roster entry's —
    its model, built on the entry's settings view (the uploaded-G-code path's
    record-first resolution, generalised). Without one, the model the job was
    prepared for: its recorded override, else the configured model.
    """
    if isinstance(job, dict) and job.get("printer_id"):
        entry = settings.by_id(job["printer_id"])
        return printers.get(entry["model"], settings.view_for(entry["id"]))
    key = job.get("printer_model") if isinstance(job, dict) else None
    if not key or key == printers.current().key:
        return printers.current()
    return printers.get(key)


def _check_tool_assignments(manifest, rows, selections):
    """One colour per toolhead (#346): prompts whose source colours differ must
    not share a toolhead. Prompts sharing a source colour may share one."""
    from .source_colors import prompt_source_colour
    seen = {}
    for row, selection in zip(rows, selections):
        slot = selection['slot']
        colour = prompt_source_colour(manifest, row)
        if slot in seen and seen[slot] != colour:
            raise ValueError(f"toolhead {slot} is already assigned another color in this job; "
                             "one toolhead prints one color per job")
        seen[slot] = colour


def intake_color_choices(job_path, state, mode=None, slots=None, interactive=False):
    """Normal intake gate: ask per part/palette and persist exact-source choices.

    Noninteractive callers receive a structured button-ready question packet.
    They resume the same JOB via slice; no JSON file authoring is required.
    """
    from .source_colors import inventory, prompts, canonical_prompts, validate_decisions, distinct_source_colours
    path = Path(job_path)
    job = json.loads(path.read_text())
    manifest = inventory(job['downloaded_file'])
    if not manifest.get('source_sha256'):
        manifest['source_sha256'] = file_sha256(job['downloaded_file'])
    adapter = _job_adapter(job)
    multi_tool = adapter.multi_tool
    # The slot questions below are built from the record's slot map, so they serve the
    # record-bound multi_slot printers (and, tool-shaped, the multi_tool pair). A Bambu
    # AMS model is multi_slot by class (#671) but not record-bound: its filament slots
    # are the file's own, matched to reported AMS trays at print start, so a source that
    # needs slot choices is refused here exactly as it was while the model was "single".
    if not multi_tool and not (adapter.multi_slot and adapter.slots_bound_by_record):
        if manifest['requires_choices']:
            raise MultiColorUnsupported(
                f"{adapter.label} has a single extruder; this source has several parts or colors that need a "
                "slot choice. Slice a single-color model or use a multi-slot printer")
        return True
    questions = canonical_prompts(manifest) if job.get('color_schema') == 'part_slots_v1' else prompts(manifest)
    available = []
    for slot, label in sorted(state.get('filament_slots', {}).items()):
        selection = resolve_filament_selection(state, dict(label, slot=int(slot)))
        noun = 'Toolhead' if multi_tool else 'Slot'
        available.append({'label': f"{noun} {slot}: {selection['color']} {selection['material']} (configured label)",
                          'selection': selection})
    job['color_manifest'], job['color_prompts'] = manifest, questions
    job['color_questions'] = [dict(q, choices=available) for q in questions]
    write_job(path.parent, job)
    if not manifest['requires_choices']:
        return True
    if mode is None and job.get('color_decisions'):
        validate_decisions(manifest, job['color_decisions'], state, resolve_filament_selection)
        return True
    packet = {'status': 'COLOR_CHOICES_REQUIRED', 'job_json': str(path),
              'source_sha256': manifest['source_sha256'],
              'question': 'Preserve colors per part/source filament, or explicitly print everything in one color?',
              'choices': ['Preserve colors per part/source filament', 'Print everything in one color'],
              'questions': job['color_questions'],
              'next_action': 'Present the source-bound questions once through the internal color workflow and persist selections to this same job. Never ask the user to supply a filesystem path, raw source index or command.',
              'resume': 'slice JOB.json --approved --color-mode per_part --color-slot SLOT (repeat for each question)'}
    if mode is None:
        print(json.dumps(packet, indent=2))
        if not interactive:
            return False
        answer = input('Color mode (per_part or single_color; blank stops): ').strip()
        if not answer:
            return False
        mode = answer
    if mode not in ('per_part', 'single_color'):
        raise ValueError('color mode must be per_part or single_color')
    if multi_tool and mode == 'per_part':
        # One colour per toolhead (#346): more distinct source colours than
        # toolheads can never be assigned. (single_color stays available:
        # one toolhead prints everything.)
        colours = distinct_source_colours(manifest)
        if len(colours) > adapter.tool_count:
            raise MultiColorUnsupported(
                f"{adapter.label} has {adapter.tool_count} toolheads and prints one color per toolhead; "
                f"this source uses {len(colours)} colors. Reduce the source to at most "
                f"{adapter.tool_count} colors or use a printer with more toolheads")
    rows = questions if mode == 'per_part' else [
        {'question': f"Choose one {'toolhead' if multi_tool else 'slot'} for ALL parts and source colors"}]
    if slots is None and interactive:
        print('\n'.join(choice['label'] for choice in available))
        slots = [int(input(row['question'] + ': ')) for row in rows]
    if not slots or len(slots) != len(rows):
        raise ValueError('supply one --color-slot for every displayed question (one for single_color)')
    selections = [resolve_filament_selection(state, dict(state.get('filament_slots', {}).get(str(slot), {}), slot=slot)) for slot in slots]
    if multi_tool and mode == 'per_part':
        _check_tool_assignments(manifest, rows, selections)
    decisions = {'source_sha256': manifest['source_sha256'], 'mode': mode}
    if job.get('color_schema') == 'part_slots_v1':
        decisions['schema'] = 'part_slots_v1'
    if mode == 'single_color':
        decisions['selection'] = selections[0]
    else:
        decisions['choices'] = {row['key']: selection for row, selection in zip(rows, selections)}
    set_color_decisions(job, validate_decisions(manifest, decisions, state, resolve_filament_selection))
    write_job(path.parent, job)
    return True


class ImplicitPlateSplit(Exception):
    """A single implicit plate could not merge; the caller splits by slot."""

    def __init__(self, verdict):
        super().__init__(verdict.get('reason'))
        self.verdict = verdict


def single_color_groups(job, manifest):
    """Split an exact per-part decision into one group per physical slot.

    A group is one derived plate. Part keys appear once, and a group that
    mixes slots is not representable here. Decisions stay on the source job;
    this only plans the plates.
    """
    decisions = job.get('color_decisions') or {}
    if decisions.get('mode') != 'per_part' or decisions.get('schema') != 'part_slots_v1':
        raise RuntimeError('per-part plate split requires schema part_slots_v1')
    choices = decisions.get('choices') or {}
    groups = {}
    seen = set()
    for part in manifest.get('parts') or []:
        selection = choices.get('part:' + part['key'])
        if not selection:
            raise RuntimeError('per-part decision is missing a source part')
        slot = selection.get('slot')
        if part['key'] in seen:
            raise RuntimeError('source inventory repeats a part key')
        seen.add(part['key'])
        groups.setdefault(slot, []).append(part['key'])
    if not groups:
        raise RuntimeError('per-part decision has no printable parts')
    return [{'slot': slot, 'part_keys': keys} for slot, keys in sorted(groups.items(), key=lambda row: row[0])]


def _per_part_view(manifest, decisions):
    """Exact per-part decisions for the geometry exporters.

    A single_color decision is expanded in memory only; the persisted job
    decisions (and their digest) stay exactly what the user chose.
    """
    if decisions['mode'] == 'per_part':
        return decisions
    from .source_colors import canonical_prompts
    selection = decisions['selection']
    return {'schema': 'part_slots_v1', 'mode': 'per_part', 'source_sha256': manifest['source_sha256'],
            'choices': {row['key']: selection for row in canonical_prompts(manifest)}}


def _part_slots(view, manifest):
    if manifest.get('painted'):
        # Painted regions pick slots per source filament, not per part. Every
        # chosen filament slot is in play; part keys map to their base filament.
        choices = view['choices']
        out = {part['key']: choices[f"filament:{part['source_extruder']}"]['slot']
               for part in manifest['parts'] if f"filament:{part['source_extruder']}" in choices}
        out.update({key: row['slot'] for key, row in choices.items() if key.startswith('filament:')})
        return out
    return {part['key']: view['choices']['part:' + part['key']]['slot'] for part in manifest['parts']}


def _job_fit(job, brim_override):
    """Operator-chosen scale/orientation for per-part plates.

    Returns (fit, supports, brim). fit is None at authored size. The same
    verified option the whole-model path honors is applied to every slot
    plate: rotation, uniform scale, and the supports/brim it was offered with.
    """
    scale = job.get('scale')
    orientation = job.get('orientation') or {}
    rotation = orientation.get('rotation')
    if not rotation and (scale is None or float(scale) >= 1.0):
        if brim_override is None and job.get('fit_brim_policy'):
            brim_override = job['fit_brim_policy'].get('width_mm', 0)
        return None, None, brim_override
    fit = {'scale': float(scale) if scale is not None else 1.0, 'rotation': rotation}
    supports = bool(orientation.get('supports')) if rotation else None
    if brim_override is None and job.get('fit_brim_policy'):
        brim_override = job['fit_brim_policy'].get('width_mm', 0)
    if rotation and brim_override is None and orientation.get('brim'):
        brim_override = 5
    return fit, supports, brim_override


def _merge_edge(brim):
    """Bed-edge margin for merge/rearrange packing: the merge edge margin plus the job's brim.

    The single-plate arranger already adds the brim (full_size_one_plate);
    the merge planner and the per-slot rearranger must too, or a brim on a
    part packed at the bare edge margin (1 mm with layout.spacing minimum)
    crosses the bed edge.
    """
    from .plate_merge import EDGE_MARGIN_MM
    return EDGE_MARGIN_MM + float(brim or 0)


def _rearranged_group_placement(src, manifest, group, bed, brim=0):
    """export_single_color_plate kwargs that place one rearranged slot plate at 100%."""
    from . import plate_merge
    from .orca_adapter import _yaw
    keys = set(group['part_keys'])
    object_ids = {str(part['object_id']) for part in manifest['parts'] if part['key'] in keys}
    arranged = plate_merge.arrange_group(src, object_ids, bed, edge=_merge_edge(brim))
    if arranged is None:
        raise RuntimeError(f"the slot {group['slot']} parts do not fit one {bed} mm plate at 100% even "
                           'rearranged; choose split or shrink instead. Nothing sliced')
    kwargs = {'item_shifts': arranged['shifts']}
    if arranged['yaw_by_object']:
        kwargs['fit_by_object'] = {oid: {'rotation': _yaw(deg)} for oid, deg in arranged['yaw_by_object'].items()}
    return kwargs


def _plates_for(source, manifest):
    """Source plates, or the implicit single plate a plate-less 3MF holds (as slicing treats it)."""
    try:
        return plate_manifest(source)['plates']
    except RuntimeError:
        return [{'id': 1, 'name': 'Plate 1', 'status': 'planned',
                 'object_ids': sorted({int(p['object_id']) for p in manifest['parts']})}]


def full_size_one_plate(source, state, decisions=None):
    """A 100%-scale, one-plate rearrangement the slicer will honor exactly, or None.

    This is the cheap first fit check after the author's layout fails the bed:
    no Orca probe runs. It answers with the planner the slice path uses, so an
    arrangement it reports is one slicing realizes:

    * per-part color decisions on a 3MF slice through the merge planner
      (``plate_merge.plan``) with the rearrangement flag, which keeps the
      merge spacing, bed-edge margin and prime-tower room for multi-slot jobs;
    * a single-color or generic single-plate source slices through
      ``geometry_layout.arrange_and_export``, so it is checked with
      ``probe_single_plate_rearrange`` at the same defaults.

    Multi-plate single-color sources and anything the planners cannot place
    return None, and intake falls back to the full fit search.
    """
    from . import plate_merge
    from .geometry_layout import probe_single_plate_rearrange
    source = Path(source)
    st = dict(state or {})
    st.setdefault('bed_mm', [BED_MM, BED_MM])
    st.setdefault('max_print_height_mm', MAX_HEIGHT_MM)
    bed = (float(st['bed_mm'][0]), float(st['bed_mm'][1]), float(st['max_print_height_mm']))
    mode = (decisions or {}).get('mode')
    if mode == 'per_part':
        if source.suffix.lower() != '.3mf':
            return None
        from .source_colors import inventory
        manifest = inventory(source)
        if len(manifest.get('parts') or []) < 2:
            return None
        view = _per_part_view(manifest, decisions)
        slots = _part_slots(view, manifest)
        flagged = {'full_size_rearrange_plan': {'rearrange_plate': True}}
        verdict = plate_merge.plan(source, manifest, _plates_for(source, manifest), slots, st,
                                   bed=int(bed[0]), job=flagged,
                                   edge=_merge_edge(st.get('brim_override')))
        if verdict.get('mode') != 'merge':
            return None
        boxes = list((verdict.get('placed_bounds_mm') or {}).values())
        span = ([round(max(b[0][1] for b in boxes) - min(b[0][0] for b in boxes), 2),
                 round(max(b[1][1] for b in boxes) - min(b[1][0] for b in boxes), 2),
                 round(max(b[2][1] for b in boxes), 2)] if boxes else None)
        return {'id': 'rearrange_100', 'scale': 1.0, 'rearrange_plate': True, 'via': 'merge_planner',
                'part_count': len(verdict.get('objects_out') or []),
                'turned_parts': len(verdict.get('yaw_by_object') or {}),
                'slots': verdict.get('slots') or [], **({'size_mm': span} if span else {})}
    if source.suffix.lower() == '.3mf':
        try:
            if len(plate_manifest(source).get('plates') or []) > 1:
                return None  # multi-plate sources slice through the merge planner, which needs decisions
        except RuntimeError:
            pass
    from .geometry_layout import DEFAULT_MARGIN_MM as arrange_edge
    # The same margin the slice will arrange with: layout.arrange_edge_mm plus any brim.
    plan = probe_single_plate_rearrange(source, bed=bed,
                                        margin=arrange_edge + float(st.get('brim_override') or 0))
    if plan is None:
        return None
    return {**plan, 'via': 'arrange_and_export'}


def per_part_fit_options(source, decisions, options, bed=BED_MM):
    """Keep only the fit options every per-part slot plate can honor.

    Each slot group is exported with the option applied, exactly as slicing
    will. An option that crops, lifts a part off the bed, or otherwise fails
    for any slot is dropped, so intake never offers a choice it cannot slice.
    """
    import tempfile
    from .color_geometry import export_single_color_plate
    from .source_colors import inventory
    manifest = inventory(source)
    view = _per_part_view(manifest, decisions)
    plates = plate_manifest(source).get('plates', []) if Path(source).suffix.lower() == '.3mf' else []
    object_plate = {str(o): plate['id'] for plate in plates for o in plate.get('object_ids', [])} if plates else {}
    groups = {}
    for part in manifest['parts']:
        p_id = object_plate.get(str(part['object_id']), 1) if plates else 1
        slot = view['choices']['part:' + part['key']]['slot']
        groups.setdefault((p_id, slot) if plates else slot, []).append(part['key'])

    def honored(fit):
        with tempfile.TemporaryDirectory(prefix='per-part-fit-') as tmp:
            for group_key, keys in groups.items():
                tag = f"p{group_key[0]}_s{group_key[1]}" if isinstance(group_key, tuple) else f"slot{group_key}"
                try:
                    export_single_color_plate(source, Path(tmp) / f'{tag}.3mf', view, keys, bed=bed, fit=fit)
                except RuntimeError:
                    return False
        return True

    scales = [s for s in options.get('scales') or [] if honored({'scale': s})]
    rotations = [r for r in options.get('rotations') or []
                 if r.get('rotation') and honored({'scale': r.get('scale'), 'rotation': r['rotation']})]
    details = []
    prior = {str(row.get('object_id')): row for row in options.get('parts') or [] if row.get('object_id')}
    try:
        with zipfile.ZipFile(source) as archive:
            for part in manifest.get('parts', []):
                box = __import__('print_handy.core.color_geometry', fromlist=['_world_bounds'])._world_bounds(
                    archive, {str(part['object_id'])})[0]
                row = {'name': part.get('name') or f"Object {part['object_id']}",
                       'extent_mm': [round(box[i][1] - box[i][0], 1) for i in range(3)]}
                # Keep the per-part fit evaluation from fit_options (#76), so a
                # part that fits no plate alone stays blocking in per-part mode.
                fit = prior.get(str(part['object_id']))
                if fit:
                    row.update({k: fit[k] for k in ('object_id', 'fits_full_size', 'best_yaw_deg',
                                                    'best_extent_mm', 'usable_mm', 'height_limit_mm',
                                                    'blocking_reason', 'orientations_checked',
                                                    'placement_mm') if k in fit})
                details.append(row)
    except (OSError, zipfile.BadZipFile, RuntimeError, KeyError):
        details = []
    return {**options, 'scales': scales, 'rotations': rotations, 'parts': details}


def _gcode_object_count(text):
    return len(set(re.findall(r'^; printing object (.+?)(?: id:\d+)?(?: copy \d+)?\s*$', text, re.M)))


def slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override):
    """Slice every object of a multi-plate 3MF. Disk only, never the printer.

    job['plate_mode'] 'merge' (default) packs all objects on one bed and
    slices once. 'separate' (or any split reason) slices one G-code per
    source plate and slot. Every split carries a reason in job['plate_merge'].
    """
    from . import plate_merge
    if not decisions:
        raise RuntimeError('part/color choices required; run color-plan and color-select before slicing')
    view = _per_part_view(manifest, decisions)
    slots = _part_slots(view, manifest)
    implicit = False
    try:
        plates = plate_manifest(src)['plates']
    except RuntimeError:
        # A single-plate 3MF may carry no plate assignments; it is one plate
        # holding every object. A multi-plate source must name its plates.
        if job.get('audit', {}).get('plates', 0) and int(job['audit']['plates']) > 1:
            raise
        implicit = True
        plates = [{'id': 1, 'name': 'Plate 1', 'status': 'planned',
                   'object_ids': sorted({int(p['object_id']) for p in manifest['parts']})}]
    assigned = {str(o) for plate in plates for o in plate['object_ids']}
    missing = sorted({part['object_id'] for part in manifest['parts']} - assigned, key=str)
    if missing:
        raise RuntimeError(f'multi-plate 3MF objects {",".join(missing)} belong to no plate; no slicing attempted')
    work = Path(job_path).parent
    mode = job.get('plate_mode', 'merge')
    if mode not in ('merge', 'separate'):
        raise RuntimeError("plate_mode must be 'merge' or 'separate'")
    verdict = {'mode': 'split', 'reason': 'Print parts separately was chosen', 'plates_in': len(plates),
               'objects_in': sorted({p['object_id'] for p in manifest['parts']}, key=str)}
    fit, _, _ = _job_fit(job, brim_override)
    if fit is not None:
        # Merge shifts are planned at authored size. A resized or rotated job
        # slices one fitted plate per slot; the whole model was verified to fit.
        verdict['reason'] = 'Resized per-part job: one fitted plate per slot'
    elif mode == 'merge':
        _, _, plan_brim = _job_fit(job, brim_override)
        verdict = plate_merge.plan(src, manifest, plates, slots, st, job=job, edge=_merge_edge(plan_brim))
        if verdict['mode'] == 'merge':
            merged_slice = (_slice_merged_multi_tool if _job_adapter(job).multi_tool else _slice_merged)
            try:
                result = merged_slice(job, job_path, src, manifest, decisions, view, verdict, st, brim_override, work)
            except plate_merge.SplitNeeded as exc:
                verdict = {**verdict, 'mode': 'split', 'reason': str(exc)}
            else:
                return result
    job['plate_merge'] = {k: v for k, v in verdict.items()
                          if k not in ('shifts', 'placed_bounds_mm', 'yaw_by_object')}
    if _job_adapter(job).multi_tool:
        # The split fallbacks below are IFS-shaped (per-slot plates finalized
        # through the native slot contract). A toolhead printer prints a
        # multi-colour job as one merged plate or not at all (#346).
        raise ImplicitPlateSplit(job['plate_merge'])
    if manifest.get('painted') and len(set(slots.values())) > 1:
        # Painted colors live inside one mesh; a per-slot split would print the
        # same body twice. Split by authored plate instead, each one multicolor.
        print('SPLIT painted 3MF into per-plate multicolor G-code: ' + str(verdict['reason']))
        return _slice_painted_plates(job, job_path, src, manifest, decisions, view, plates, st, brim_override, work)
    if implicit or len(plates) == 1:
        # One plate that cannot merge splits by slot, the legacy single-color path.
        raise ImplicitPlateSplit(job['plate_merge'])
    print('SPLIT multi-plate 3MF into per-plate G-code: ' + str(verdict['reason']))
    return _slice_per_plate(job, job_path, src, manifest, decisions, view, plates, st, brim_override, work)


PAINTED_SCALE_STEPS = (1.0, 0.95, 0.9, 0.87, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5)


def painted_plate_scale(src, view, object_ids, ceiling=1.0):
    """Largest ladder scale <= ceiling at which this plate plus its prime tower
    fits the bed. Pure geometry export, no slicer, no printer."""
    import tempfile
    from .color_geometry import export_color_geometry
    with tempfile.TemporaryDirectory(prefix='painted-fit-') as tmp:
        for scale in PAINTED_SCALE_STEPS:
            if scale > ceiling + 1e-9:
                continue
            try:
                _, geometry = export_color_geometry(src, Path(tmp) / 'g.3mf', view, object_ids=object_ids, scale=scale)
            except RuntimeError:
                continue
            if all(0 <= ax[0] and ax[1] <= BED_LIMIT_MM for row in geometry['parts'] for ax in row['bounds_mm'][:2]):
                return scale
    return None


BED_LIMIT_MM = BED_MM


def painted_fit(src, view, plates, ceiling=1.0):
    """One shared scale for every authored plate of a painted source."""
    scale, blocking = ceiling, None
    for plate in plates:
        ids = {str(o) for o in plate['object_ids']}
        fitted = painted_plate_scale(src, view, ids, scale)
        if fitted is None:
            return {'scale': None, 'blocking_plate': plate['id'], 'plates': len(plates)}
        if fitted < scale:
            scale, blocking = fitted, plate['id']
    return {'scale': scale, 'blocking_plate': blocking, 'plates': len(plates)}


def painted_fit_for_job(src, decisions):
    """Intake helper: shared painted-plate scale for an exact-source decision."""
    from .source_colors import inventory
    manifest = inventory(src)
    view = _per_part_view(manifest, decisions)
    try:
        plates = plate_manifest(src)['plates']
    except RuntimeError:
        plates = [{'id': 1, 'object_ids': sorted({str(p['object_id']) for p in manifest['parts']})}]
    plates = [p for p in plates if any(str(o) in {str(x['object_id']) for x in manifest['parts']} for o in p['object_ids'])]
    return painted_fit(src, view, plates)


def _slice_painted_plates(job, job_path, src, manifest, decisions, view, plates, st, brim_override, work):
    """One multicolor G-code per authored plate of a triangle-painted 3MF.

    Paint is remapped to the chosen slots inside every mesh, so no plate ever
    prints a body in the wrong color. Every plate uses one scale so parts
    still fit together: job['scale'] caps it (default full size), otherwise it
    is the largest ladder scale at which every plate and its prime tower fit.
    """
    import uuid
    from .orca_adapter import slice_model
    source_sha = file_sha256(src); decision_sha = decisions_digest(decisions)
    fit = painted_fit(src, view, plates, float(job.get('scale') or 1.0))
    scale = fit['scale']
    if scale is None:
        raise RuntimeError(f"painted plate {fit['blocking_plate']} does not fit the bed with a prime tower at 50% or larger; no slicing attempted")
    artifacts = []
    for plate in plates:
        ids = {str(o) for o in plate['object_ids']}
        keys = [p['key'] for p in manifest['parts'] if str(p['object_id']) in ids]
        if not keys:
            continue
        slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
        out, provenance, configs = slice_model(src, slice_dir, st, brim_override, color_decisions=view,
                                               object_ids=ids, scale=None if scale >= 1.0 else scale)
        geometry = provenance.get('geometry') or {}
        if sorted(r['part_key'] for r in geometry.get('parts', [])) != sorted(keys):
            raise RuntimeError(f"painted plate {plate['id']} dropped or duplicated a source part")
        check = verify_gcode_offline(out, st)
        tools = provenance.get('tool_mapping') or {}
        artifacts.append({'plate_id': plate['id'], 'slot': sorted(set(tools.values())),
                          'binding_key': f"{plate['id']}:painted", 'part_keys': keys,
                          'scale': scale, 'gcode': str(out), 'offline_check': check,
                          'estimate_s': gcode_estimate_s(Path(out).read_text(errors='ignore')),
                          'provenance': provenance, 'slice_config': [str(p) for p in configs]})
    if not artifacts:
        raise RuntimeError('painted source produced no plates')
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest):
        raise RuntimeError('source, decisions or slice recipe changed during slicing; artifact not made ready')
    job['derived_plates'] = artifacts
    job['plate_merge'] = {'mode': 'split', 'painted': True, 'plates_in': len(plates), 'plates_out': len(artifacts),
                          'reason': 'painted multicolor plates sliced separately, one multicolor G-code each',
                          'scales': {str(a['plate_id']): a['scale'] for a in artifacts}, 'fits_bed': True,
                          'bed_mm': int(st['bed_mm'][0])}
    job['artifact_binding'] = {**_recipe_binding(job), 'source_sha256': source_sha, 'decisions_sha256': decision_sha,
                               'gcode_sha256': {a['binding_key']: file_sha256(a['gcode']) for a in artifacts}}
    job.update({'gcode': artifacts[0]['gcode'], 'gcode_verified': True,
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    for a in artifacts:
        print(f"SLICED painted plate {a['plate_id']} at {round(a['scale'] * 100)}% (not uploaded): {a['gcode']}")
    return [a['gcode'] for a in artifacts]


def _slice_merged(job, job_path, src, manifest, decisions, view, verdict, st, brim_override, work):
    import uuid
    from . import plate_merge
    from .orca_adapter import slice_model
    source_sha = file_sha256(src); decision_sha = decisions_digest(decisions)
    slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
    slots = sorted(set(_part_slots(view, manifest).values()))
    yaw_by_object = verdict.get('yaw_by_object') or {}
    if not isinstance(yaw_by_object, dict):
        raise RuntimeError('merge plan yaw_by_object must be a mapping of object id to degrees')
    fit_by_object = {}
    if yaw_by_object:
        # A recorded merge turn (#221 R2b) is realized only as the
        # exporters' per-object fit composed with the plan's recorded
        # shifts — the proved placement. Fail closed on anything else
        # instead of slicing the objects as authored.
        if not verdict.get('shifts'):
            raise RuntimeError('merge plan records a turn without the recorded shifts that realize it; '
                               'refusing to slice it as authored')
        from .orca_adapter import _yaw
        unknown_yaw = sorted(set(yaw_by_object) - {str(p['object_id']) for p in manifest['parts']}, key=str)
        if unknown_yaw:
            raise RuntimeError(f"merge plan records a turn for unknown object(s) {','.join(unknown_yaw)}")
        for oid in sorted(yaw_by_object, key=str):
            try:
                value = float(yaw_by_object[oid])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f'merge plan turn for object {oid} must be numeric, '
                                   f'got {yaw_by_object[oid]!r}') from exc
            if not math.isfinite(value) or not 0 < value <= 90:
                raise RuntimeError(f'merge plan turn must be between 0 and 90 degrees, got {value!r}')
            fit_by_object[str(oid)] = {'rotation': _yaw(value)}
    if len(slots) == 1 and not manifest.get('painted'):
        from .color_geometry import export_single_color_plate
        derived = work / 'derived'; derived.mkdir(exist_ok=True)
        plate = derived / 'merged.3mf'
        report = export_single_color_plate(src, plate, view, [p['key'] for p in manifest['parts']], bed=int(st['bed_mm'][0]),
                                           slot_colors=st.get('filament_slots') or {}, item_shifts=verdict['shifts'],
                                           **({'fit_by_object': fit_by_object} if fit_by_object else {}))
        if fit_by_object:
            # Re-read the derived plate: every turned object must have
            # landed at its rotated authored bounds plus its recorded
            # shift, with the rotated extents — the shared split-plate
            # proof (#221 R2) applied to the merged plate.
            from .color_geometry import _world_bounds as _bounds_of
            from .color_geometry import _world_points as _points_of
            from .orca_adapter import _rotate as _rotate_point
            shifts = {str(k): v for k, v in verdict['shifts'].items()}
            with zipfile.ZipFile(src) as archive:
                turned_points = {oid: _points_of(archive, {oid}) for oid in fit_by_object}
            with zipfile.ZipFile(plate) as archive:
                landed_boxes = {oid: _bounds_of(archive, {oid})[0] for oid in fit_by_object}
            for oid, entry in fit_by_object.items():
                rotated = [_rotate_point(point, entry['rotation']) for point in turned_points[oid]]
                base_min = [min(p[a] for p in rotated) for a in range(3)]
                base_extent = [max(p[a] for p in rotated) - base_min[a] for a in range(3)]
                landed = landed_boxes[oid]
                landed_extent = [landed[a][1] - landed[a][0] for a in range(3)]
                values = shifts[oid]
                if any(abs(landed[a][0] - (base_min[a] + float(values[a]))) > 2e-3 for a in range(3)) \
                        or any(abs(got - want) > 2e-3 for got, want in zip(landed_extent, base_extent)):
                    raise RuntimeError(f'merged plate did not land at the recorded turn for object {oid}; '
                                       'refusing to slice it as turned')
        out, provenance, configs = slice_model(plate, slice_dir, st, brim_override, preserve_layout=True, filament_slot=slots[0])
        material = st.get('filament_slots', {}).get(str(slots[0]), {}).get('material')
        if not material:
            raise RuntimeError(f'merged plate physical IFS slot {slots[0]} has no verified material')
        finalize_multicolor(out, None, st, {'0': slots[0]})
        placed_ids = sorted(report['object_ids'], key=str)
        extra = {'plate': str(plate), 'plate_report': report}
    else:
        # Painted single-color choices still go through the paint-preserving
        # exporter, so pass the per-part view (it is exactly the user's choice).
        out, provenance, configs = slice_model(src, slice_dir, st, brim_override,
                                               color_decisions=view if manifest.get('painted') else decisions,
                                               item_shifts=verdict['shifts'],
                                               **({'fit_by_object': fit_by_object} if fit_by_object else {}))
        geometry = provenance.get('geometry') or {}
        placed_keys = sorted(row['part_key'] for row in geometry.get('parts', []))
        if placed_keys != sorted(p['key'] for p in manifest['parts']):
            raise RuntimeError('merged geometry dropped or duplicated a source part')
        if fit_by_object:
            # This route has no derived plate file to re-read: the
            # multicolor exporter's report bounds are computed from the
            # exact vertices it wrote, so they are the strongest available
            # proof — every turned object's placed bounds must equal its
            # rotated authored bounds plus its recorded shift (#221 R2b).
            from .color_geometry import _world_points as _points_of
            from .orca_adapter import _rotate as _rotate_point
            object_of_key = {p['key']: str(p['object_id']) for p in manifest['parts']}
            placed_by_object = {}
            for row in geometry.get('parts', []):
                oid = object_of_key[row['part_key']]
                box = row['bounds_mm']
                if oid in placed_by_object:
                    prev = placed_by_object[oid]
                    box = [(min(prev[a][0], box[a][0]), max(prev[a][1], box[a][1])) for a in range(3)]
                placed_by_object[oid] = box
            shifts = {str(k): v for k, v in verdict['shifts'].items()}
            with zipfile.ZipFile(src) as archive:
                turned_points = {oid: _points_of(archive, {oid}) for oid in fit_by_object}
            for oid, entry in fit_by_object.items():
                rotated = [_rotate_point(point, entry['rotation']) for point in turned_points[oid]]
                base_min = [min(p[a] for p in rotated) for a in range(3)]
                base_extent = [max(p[a] for p in rotated) - base_min[a] for a in range(3)]
                landed = placed_by_object[oid]
                landed_extent = [landed[a][1] - landed[a][0] for a in range(3)]
                values = shifts[oid]
                if any(abs(landed[a][0] - (base_min[a] + float(values[a]))) > 2e-3 for a in range(3)) \
                        or any(abs(got - want) > 2e-3 for got, want in zip(landed_extent, base_extent)):
                    raise RuntimeError(f'merged geometry did not land at the recorded turn for object {oid}; '
                                       'refusing to slice it as turned')
        placed_ids = sorted({p['object_id'] for p in manifest['parts'] if p['key'] in placed_keys}, key=str)
        extra = {'geometry_report': geometry}
    text = Path(out).read_text(errors='ignore')
    estimate = gcode_estimate_s(text)
    if estimate is not None and estimate > plate_merge.MAX_MERGED_S:
        raise plate_merge.SplitNeeded(f'merged estimate {human_dur(estimate)} exceeds the {plate_merge.MAX_MERGED_S / 3600:g} h limit')
    if placed_ids != verdict['objects_in']:
        raise RuntimeError('merged output does not account for every source object')
    labelled = _gcode_object_count(text)
    if labelled and labelled != len(manifest['parts']):
        raise RuntimeError(f'merged G-code prints {labelled} objects but the source has {len(manifest["parts"])}')
    check = verify_gcode_offline(out, st)
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest):
        raise RuntimeError('source, decisions or slice recipe changed during slicing; artifact not made ready')
    summary = {'mode': 'merge', 'reason': None, 'plates_in': verdict['plates_in'], 'objects_in': len(verdict['objects_in']),
               'objects_out': len(placed_ids), 'parts_in': len(manifest['parts']), 'bed_mm': verdict['bed_mm'],
               'spacing_mm': verdict['spacing_mm'], 'fits_bed': True, 'slots': slots,
               'placed_bounds_mm': verdict['placed_bounds_mm'], 'estimate_s': estimate,
               'estimate': human_dur(estimate) if estimate else None, 'gcode_objects_labelled': labelled or None,
               **({'yaw_by_object': yaw_by_object} if yaw_by_object else {})}
    job.pop('derived_plates', None); job.pop('refused_plates', None); job.pop('multipart_plan', None)
    job['plate_merge'] = summary
    job['artifact_binding'] = {**_recipe_binding(job), 'source_sha256': source_sha, 'decisions_sha256': decision_sha, 'gcode_sha256': file_sha256(out)}
    job.update({'slice_config': [str(p) for p in configs], 'gcode': str(out), 'provenance': provenance,
                'gcode_verified': True, 'offline_check': check, 'merged': extra,
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    print(f"SLICED merged {summary['objects_in']} objects from {summary['plates_in']} plates onto one bed (not uploaded): {out}")
    return out


def _slice_merged_multi_tool(job, job_path, src, manifest, decisions, view, verdict, st, brim_override, work):
    """One merged G-code for a multi_tool printer (#346).

    The merged-colour exporter runs in identity mode: slicer tool T(n-1) is
    toolhead n, the position the operator picked for that colour. The picks
    are checked against the adapter's identity-only tool-mapping contract
    unchanged, and Orca slices through the model's own multi-extruder
    templates — there is no IFS finalize step on a toolhead printer.
    """
    import uuid
    from . import plate_merge
    from .orca_adapter import slice_model
    adapter = _job_adapter(job)
    source_sha = file_sha256(src); decision_sha = decisions_digest(decisions)
    slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
    slots = sorted(set(_part_slots(view, manifest).values()))
    yaw_by_object = verdict.get('yaw_by_object') or {}
    if not isinstance(yaw_by_object, dict):
        raise RuntimeError('merge plan yaw_by_object must be a mapping of object id to degrees')
    fit_by_object = {}
    if yaw_by_object:
        if not verdict.get('shifts'):
            raise RuntimeError('merge plan records a turn without the recorded shifts that realize it; '
                               'refusing to slice it as authored')
        from .orca_adapter import _yaw
        unknown_yaw = sorted(set(yaw_by_object) - {str(p['object_id']) for p in manifest['parts']}, key=str)
        if unknown_yaw:
            raise RuntimeError(f"merge plan records a turn for unknown object(s) {','.join(unknown_yaw)}")
        for oid in sorted(yaw_by_object, key=str):
            try:
                value = float(yaw_by_object[oid])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f'merge plan turn for object {oid} must be numeric, '
                                   f'got {yaw_by_object[oid]!r}') from exc
            if not math.isfinite(value) or not 0 < value <= 90:
                raise RuntimeError(f'merge plan turn must be between 0 and 90 degrees, got {value!r}')
            fit_by_object[str(oid)] = {'rotation': _yaw(value)}
    out, provenance, configs = slice_model(src, slice_dir, st, brim_override,
                                           color_decisions=view if manifest.get('painted') else decisions,
                                           item_shifts=verdict['shifts'],
                                           **({'fit_by_object': fit_by_object} if fit_by_object else {}))
    mapping = {str(tool): int(slot) for tool, slot in (provenance.get('tool_mapping') or {}).items()}
    # The recorded mapping must pass the adapter's identity contract exactly
    # as recorded: tool T(n-1) prints with toolhead n, nothing remapped (#346).
    adapter.normalize_tool_mapping(mapping, st)
    geometry = provenance.get('geometry') or {}
    placed_keys = sorted(row['part_key'] for row in geometry.get('parts', []))
    if placed_keys != sorted(p['key'] for p in manifest['parts']):
        raise RuntimeError('merged geometry dropped or duplicated a source part')
    if fit_by_object:
        from .color_geometry import _world_points as _points_of
        from .orca_adapter import _rotate as _rotate_point
        object_of_key = {p['key']: str(p['object_id']) for p in manifest['parts']}
        placed_by_object = {}
        for row in geometry.get('parts', []):
            oid = object_of_key[row['part_key']]
            box = row['bounds_mm']
            if oid in placed_by_object:
                prev = placed_by_object[oid]
                box = [(min(prev[a][0], box[a][0]), max(prev[a][1], box[a][1])) for a in range(3)]
            placed_by_object[oid] = box
        shifts = {str(k): v for k, v in verdict['shifts'].items()}
        with zipfile.ZipFile(src) as archive:
            turned_points = {oid: _points_of(archive, {oid}) for oid in fit_by_object}
        for oid, entry in fit_by_object.items():
            rotated = [_rotate_point(point, entry['rotation']) for point in turned_points[oid]]
            base_min = [min(p[a] for p in rotated) for a in range(3)]
            base_extent = [max(p[a] for p in rotated) - base_min[a] for a in range(3)]
            landed = placed_by_object[oid]
            landed_extent = [landed[a][1] - landed[a][0] for a in range(3)]
            values = shifts[oid]
            if any(abs(landed[a][0] - (base_min[a] + float(values[a]))) > 2e-3 for a in range(3)) \
                    or any(abs(got - want) > 2e-3 for got, want in zip(landed_extent, base_extent)):
                raise RuntimeError(f'merged geometry did not land at the recorded turn for object {oid}; '
                                   'refusing to slice it as turned')
    placed_ids = sorted({p['object_id'] for p in manifest['parts'] if p['key'] in placed_keys}, key=str)
    text = Path(out).read_text(errors='ignore')
    estimate = gcode_estimate_s(text)
    if estimate is not None and estimate > plate_merge.MAX_MERGED_S:
        raise plate_merge.SplitNeeded(f'merged estimate {human_dur(estimate)} exceeds the {plate_merge.MAX_MERGED_S / 3600:g} h limit')
    if placed_ids != verdict['objects_in']:
        raise RuntimeError('merged output does not account for every source object')
    labelled = _gcode_object_count(text)
    if labelled and labelled != len(manifest['parts']):
        raise RuntimeError(f'merged G-code prints {labelled} objects but the source has {len(manifest["parts"])}')
    check = verify_gcode_offline(out, st)
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest):
        raise RuntimeError('source, decisions or slice recipe changed during slicing; artifact not made ready')
    summary = {'mode': 'merge', 'reason': None, 'plates_in': verdict['plates_in'], 'objects_in': len(verdict['objects_in']),
               'objects_out': len(placed_ids), 'parts_in': len(manifest['parts']), 'bed_mm': verdict['bed_mm'],
               'spacing_mm': verdict['spacing_mm'], 'fits_bed': True, 'slots': slots,
               'placed_bounds_mm': verdict['placed_bounds_mm'], 'estimate_s': estimate,
               'estimate': human_dur(estimate) if estimate else None, 'gcode_objects_labelled': labelled or None,
               **({'yaw_by_object': yaw_by_object} if yaw_by_object else {})}
    job.pop('derived_plates', None); job.pop('refused_plates', None); job.pop('multipart_plan', None)
    job['plate_merge'] = summary
    job['tool_mapping'] = mapping
    job['artifact_binding'] = {**_recipe_binding(job), 'source_sha256': source_sha, 'decisions_sha256': decision_sha, 'gcode_sha256': file_sha256(out)}
    job.update({'slice_config': [str(p) for p in configs], 'gcode': str(out), 'provenance': provenance,
                'gcode_verified': True, 'offline_check': check, 'merged': {'geometry_report': geometry},
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    print(f"SLICED merged {summary['objects_in']} objects from {summary['plates_in']} plates onto one bed (not uploaded): {out}")
    return out


def _slice_per_plate(job, job_path, src, manifest, decisions, view, plates, st, brim_override, work):
    import uuid
    fit, supports, brim_override = _job_fit(job, brim_override)
    from .color_geometry import export_single_color_plate
    from .orca_adapter import slice_model
    object_plate = {str(o): plate['id'] for plate in plates for o in plate['object_ids']}
    if fit is not None and len(plates) <= 1:
        # The chosen fit was verified for the whole model on one bed, so the
        # source plate boundaries no longer split it: one plate per slot.
        object_plate = {key: 1 for key in object_plate}
    slots = _part_slots(view, manifest)
    groups = {}
    for part in manifest['parts']:
        groups.setdefault((object_plate[str(part['object_id'])], slots[part['key']]), []).append(part['key'])
    derived = work / 'derived'; derived.mkdir(exist_ok=True)
    source_sha = file_sha256(src); decision_sha = decisions_digest(decisions)
    single_plate = len({plate for plate, _ in groups}) == 1
    artifacts = []
    refused = []
    plate_scales = job.get('plate_scales') or {}
    for (plate_id, slot), keys in sorted(groups.items()):
        current_fit = fit
        if str(plate_id) in plate_scales:
            psc = float(plate_scales[str(plate_id)])
            current_fit = {'scale': psc} if psc < 1.0 else None
        plate = derived / f'plate{plate_id}_slot{slot}.3mf'
        try:
            if not st.get('filament_slots', {}).get(str(slot), {}).get('material'):
                raise RuntimeError(f'derived plate physical IFS slot {slot} has no verified material')
            report = export_single_color_plate(src, plate, view, keys, bed=int(st['bed_mm'][0]), slot_colors=st.get('filament_slots') or {}, **({'fit': current_fit} if current_fit is not None else {}))
            slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
            out, provenance, configs = slice_model(plate, slice_dir, st, brim_override, preserve_layout=True, filament_slot=slot, **({'supports': supports} if supports is not None else {}))
            finalize_multicolor(out, None, st, {'0': slot})
            check = verify_gcode_offline(out, st)
        except RuntimeError as exc:
            # One plate failing a safety guard (e.g. an off-bed move) must not discard the
            # other plates that sliced and verified cleanly. The failed plate is recorded,
            # never published, and never launchable; it needs its own fix and reslice.
            refused.append({'slot': slot, 'plate_id': plate_id, 'binding_key': f'{plate_id}:{slot}',
                            'part_keys': keys, 'reason': ' '.join(str(exc).split())[:500]})
            print(f'REFUSED plate {plate_id} slot {slot}: {" ".join(str(exc).split())[:300]}')
            continue
        artifacts.append({'slot': slot, 'plate_id': plate_id, 'binding_key': f'{plate_id}:{slot}', 'part_keys': keys,
                          'plate': str(plate), 'gcode': str(out), 'offline_check': check, 'plate_report': report,
                          'provenance': provenance, 'slice_config': [str(path) for path in configs]})
    if not artifacts:
        raise RuntimeError('every derived plate was refused: ' + '; '.join(
            f"plate {r['plate_id']} slot {r['slot']}: {r['reason']}" for r in refused))
    covered = sorted(k for row in artifacts + refused for k in row['part_keys'])
    if covered != sorted(p['key'] for p in manifest['parts']):
        raise RuntimeError('per-plate output does not account for every source part')
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest):
        raise RuntimeError('source, decisions or slice recipe changed during slicing; artifacts not made ready')
    job['derived_plates'] = artifacts
    if refused:
        job['refused_plates'] = refused
    else:
        job.pop('refused_plates', None)
    job['artifact_binding'] = {**_recipe_binding(job), 'source_sha256': source_sha, 'decisions_sha256': decision_sha,
                               'gcode_sha256': {row['binding_key']: file_sha256(row['gcode']) for row in artifacts}}
    job.update({'gcode': artifacts[0]['gcode'], 'gcode_verified': True,
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    print('SLICED separate plates (not uploaded): ' + ', '.join(row['gcode'] for row in artifacts))
    return [row['gcode'] for row in artifacts]


def _slice_full_size_split(job, job_path, src, manifest, decisions, st):
    """Slice the exact intake-approved authored-size part groups, disk only."""
    import uuid
    from .color_geometry import export_single_color_plate
    from .orca_adapter import UP_AXES, _check_rotation, _matmul, _yaw, slice_model
    plan = job.get('full_size_split_plan') or {}
    plates = plan.get('plates') or []
    expected = {part['key'] for part in manifest.get('parts') or []}
    flattened = [key for group in plates for key in group]
    if len(flattened) < 2 or len(flattened) != len(set(flattened)) or set(flattened) != expected:
        raise RuntimeError('full-size split plan does not cover every source part exactly once')
    yaw_by_object = plan.get('yaw_by_object') or {}
    if not isinstance(yaw_by_object, dict):
        raise RuntimeError('full-size split plan yaw_by_object must be a mapping of object id to degrees')
    placement_by_object = plan.get('placement_by_object') or {}
    if not isinstance(placement_by_object, dict):
        raise RuntimeError('full-size split plan placement_by_object must be a mapping of object id to [x, y] mm')
    reorientation_by_object = plan.get('reorientation_by_object') or {}
    if not isinstance(reorientation_by_object, dict):
        raise RuntimeError('full-size split plan reorientation_by_object must be a mapping of '
                           'object id to a reorientation record')
    shifts_by_plate = plan.get('shifts_by_plate') or []
    if not isinstance(shifts_by_plate, list) or len(shifts_by_plate) not in (0, len(plates)):
        raise RuntimeError('full-size split plan shifts_by_plate must be a list parallel to plates '
                           'of per-object [dx, dy, dz] shifts (or None for a plate sliced as authored)')
    key_to_object = {part['key']: str(part['object_id']) for part in manifest.get('parts') or []}
    unknown_yaw = sorted(set(yaw_by_object) - set(key_to_object.values()))
    if unknown_yaw:
        raise RuntimeError(f"full-size split plan records a turn for unknown object(s) {','.join(unknown_yaw)}")
    unknown_placement = sorted(set(placement_by_object) - set(key_to_object.values()))
    if unknown_placement:
        raise RuntimeError(f"full-size split plan records a placement for unknown object(s) {','.join(unknown_placement)}")
    unknown_reorientation = sorted(set(reorientation_by_object) - set(key_to_object.values()))
    if unknown_reorientation:
        raise RuntimeError('full-size split plan records a reorientation for unknown '
                           f"object(s) {','.join(unknown_reorientation)}")
    unknown_shifts = sorted({oid for entry in shifts_by_plate if isinstance(entry, dict)
                             for oid in entry} - set(key_to_object.values()))
    if unknown_shifts:
        raise RuntimeError(f"full-size split plan records shifts for unknown object(s) {','.join(unknown_shifts)}")
    bed_xy = [float(v) for v in (st.get('bed_mm') or [])[:2]]
    bed_x = bed_xy[0] if bed_xy else float(st['bed_mm'][0])
    bed_y = bed_xy[1] if len(bed_xy) > 1 else bed_x
    view = _per_part_view(manifest, decisions)
    slots = sorted(set(_part_slots(view, manifest).values()))
    if len(slots) != 1:
        raise RuntimeError('full-size split plan requires one physical filament slot')
    slot = slots[0]
    if not (st.get('filament_slots') or {}).get(str(slot), {}).get('material'):
        raise RuntimeError(f'full-size split physical IFS slot {slot} has no verified material')
    work = Path(job_path).parent
    derived = work / 'derived'
    derived.mkdir(exist_ok=True)
    source_sha = file_sha256(src)
    decision_sha = decisions_digest(decisions)
    artifacts = []
    for index, keys in enumerate(plates, 1):
        plate = derived / f'full_size_split_{index}.3mf'
        turned = {key_to_object[key] for key in keys if key_to_object[key] in yaw_by_object}
        placed = {key_to_object[key] for key in keys if key_to_object[key] in placement_by_object}
        reoriented = {key_to_object[key] for key in keys
                      if key_to_object[key] in reorientation_by_object}
        fit, yaw_deg, placement_mm = None, None, None
        reorientation, supports = None, None
        shared_yaw_by_object = None
        shared_reorientation_by_object = None
        if turned:
            if len(turned) == 1 and {key_to_object[key] for key in keys} == turned:
                # The exporter turns a whole derived plate rigidly about one
                # origin, so a recorded turn for that part alone is realized
                # through the whole-plate fit (#76).
                yaw_deg = float(yaw_by_object[turned.pop()])
                if not math.isfinite(yaw_deg) or not 0 < yaw_deg <= 90:
                    raise RuntimeError(f'full-size split plan turn must be between 0 and 90 degrees, got {yaw_deg!r}')
                rad = math.radians(yaw_deg)
                c, s = math.cos(rad), math.sin(rad)
                fit = {'scale': 1.0, 'rotation': [c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0]}
            else:
                # A turn recorded for a shared plate (#221 R2) is realizable
                # only as the exporter's per-object fit composed with the
                # plate's recorded shifts — the proved shared placement.
                # Without those shifts there is nothing that realizes the
                # turn, so refuse instead of slicing the plate as authored.
                if not shifts_by_plate or shifts_by_plate[index - 1] is None:
                    raise RuntimeError('full-size split plan records a turn for a plate that is not '
                                       'that part alone, without the recorded shifts that realize '
                                       'a shared turn; refusing to slice it as authored')
                shared_yaw_by_object = {}
                for oid in sorted(turned):
                    try:
                        value = float(yaw_by_object[oid])
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError(f'full-size split plan turn for object {oid} must be '
                                           f'numeric, got {yaw_by_object[oid]!r}') from exc
                    if not math.isfinite(value) or not 0 < value <= 90:
                        raise RuntimeError('full-size split plan turn must be between 0 and 90 '
                                           f'degrees, got {value!r}')
                    shared_yaw_by_object[oid] = value
        if placed:
            # Same realizability rule as a turn: the exporter places a whole
            # derived plate rigidly, so a recorded placement is only
            # realizable for that part alone (#221).
            if len(placed) != 1 or {key_to_object[key] for key in keys} != placed:
                raise RuntimeError('full-size split plan records a placement for a plate that is not '
                                   'that part alone; refusing to slice it as authored')
            raw = placement_by_object[placed.pop()]
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                raise RuntimeError(f'full-size split plan placement must be an [x, y] pair, got {raw!r}')
            try:
                placement_mm = [float(raw[0]), float(raw[1])]
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f'full-size split plan placement must be numeric, got {raw!r}') from exc
            if (not all(math.isfinite(v) for v in placement_mm)
                    or not 0 <= placement_mm[0] < bed_x or not 0 <= placement_mm[1] < bed_y):
                raise RuntimeError(f'full-size split plan placement {placement_mm!r} is outside the '
                                   f'{bed_x:g} x {bed_y:g} mm bed; refusing to slice it as authored')
            fit = {**(fit or {'scale': 1.0}), 'placement_mm': placement_mm}
        if reoriented:
            # Every reorientation record is validated exactly as in R1: a
            # known axis, a bed turn in [0, 90] degrees (a cardinal turn, or
            # a diagonal one when no cardinal turn fit — #221 diagonal), a
            # finite rigid matrix that is
            # exactly that face change composed with the turn, and boolean
            # supports/brim decisions. A reorientation is always the object's
            # only transform record — the matrix already contains its bed
            # turn, so a yaw entry for the same object refuses. What R3 adds
            # is only how a valid record is realized beyond the solo plate:
            # alone, it goes through the exporter's whole-plate fit, with a
            # placement recorded for that same object composed into the same
            # fit (the proved exclusion-avoiding landing, #221 R3); on a
            # shared plate it goes through the exporter's per-object fit
            # channel composed with the plate's recorded shifts — the proved
            # shared placement — and without those shifts there is nothing
            # that realizes the rotation, so refuse instead of slicing the
            # plate as authored.
            validated_reorientations = {}
            for reoriented_oid in sorted(reoriented):
                if reoriented_oid in yaw_by_object:
                    raise RuntimeError('full-size split plan records a reorientation for an object '
                                       'that also records a turn; refusing to slice it as authored')
                record = reorientation_by_object[reoriented_oid]
                if not isinstance(record, dict):
                    raise RuntimeError('full-size split plan reorientation record must be a '
                                       f'mapping, got {record!r}')
                axis = record.get('axis')
                if axis not in UP_AXES:
                    raise RuntimeError('full-size split plan reorientation axis must be one of '
                                       f'{sorted(UP_AXES)}, got {axis!r}')
                try:
                    yaw_value = float(record.get('yaw_deg'))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError('full-size split plan reorientation yaw_deg must be '
                                       f"numeric, got {record.get('yaw_deg')!r}") from exc
                if not math.isfinite(yaw_value) or not 0.0 <= yaw_value <= 90.0:
                    raise RuntimeError('full-size split plan reorientation yaw_deg must be a '
                                       'bed turn between 0 and 90 degrees, '
                                       f'got {record.get("yaw_deg")!r}')
                raw_rotation = record.get('rotation')
                if not isinstance(raw_rotation, (list, tuple)) or len(raw_rotation) != 9:
                    raise RuntimeError('full-size split plan reorientation rotation must be a '
                                       f'9-value matrix, got {raw_rotation!r}')
                try:
                    matrix = [float(v) for v in raw_rotation]
                except (TypeError, ValueError) as exc:
                    raise RuntimeError('full-size split plan reorientation rotation must be '
                                       f'numeric, got {raw_rotation!r}') from exc
                if not all(math.isfinite(v) for v in matrix):
                    raise RuntimeError('full-size split plan reorientation rotation must be finite')
                try:
                    _check_rotation(matrix)
                except ValueError as exc:
                    raise RuntimeError(f'full-size split plan reorientation {exc}') from exc
                expected_matrix = _matmul(_yaw(yaw_value), UP_AXES[axis][1])
                if any(abs(a - b) > 1e-6 for a, b in zip(matrix, expected_matrix)):
                    raise RuntimeError('full-size split plan reorientation rotation is not the '
                                       'recorded face change composed with its bed turn; refusing '
                                       'to slice it as authored')
                if not isinstance(record.get('supports'), bool) \
                        or not isinstance(record.get('brim_recommended'), bool):
                    raise RuntimeError('full-size split plan reorientation record must carry '
                                       'boolean supports and brim_recommended decisions')
                validated_reorientations[reoriented_oid] = {**record, 'rotation': matrix,
                                                            'yaw_deg': yaw_value}
            if len(reoriented) == 1 and {key_to_object[key] for key in keys} == reoriented:
                # Solo (#221 R1): the exporter's whole-plate fit turns the
                # plate rigidly about one origin; a placement recorded for
                # this same object composes into the same fit (#221 R3).
                if shifts_by_plate and shifts_by_plate[index - 1] is not None:
                    raise RuntimeError('full-size split plan records a reorientation for a plate '
                                       'that also records shifts; refusing to slice it as authored')
                reorientation = validated_reorientations[next(iter(reoriented))]
                # The recorded supports decision — named in the intake offer,
                # so choosing the split approved it — is passed to the slicer
                # for this plate only; other plates keep today's default (None).
                supports = reorientation['supports']
                fit = {'scale': 1.0, 'rotation': reorientation['rotation'],
                       **({'placement_mm': placement_mm} if placement_mm is not None else {})}
            else:
                # Shared (#221 R3): realized per object through fit_by_object
                # composed with the plate's recorded shifts, validated below.
                if not shifts_by_plate or shifts_by_plate[index - 1] is None:
                    raise RuntimeError('full-size split plan records a reorientation for a plate '
                                       'that is not that part alone, without the recorded shifts '
                                       'that realize a shared reorientation; refusing to slice '
                                       'it as authored')
                shared_reorientation_by_object = validated_reorientations
                # One supports decision per plate, from the members' named
                # records: a member that needs supports turns them on for the
                # plate; when every member carries a record and none needs
                # supports, the records govern and supports stay off; when
                # non-reoriented members share the plate they keep today's
                # default (None) — a co-tenant's record never takes supports
                # away from a part that has none of its own.
                if any(row['supports'] for row in validated_reorientations.values()):
                    supports = True
                elif {key_to_object[key] for key in keys} == reoriented:
                    supports = False
        shifts_entry = shifts_by_plate[index - 1] if shifts_by_plate else None
        item_shifts = None
        if shifts_entry is not None:
            # Polygon-shared plate (#221 P2): the recorded per-object shifts
            # are the only realizable form of the proved hull placement.
            # Fail closed on anything the plan did not prove for exactly this
            # plate — never slice such a plate as authored.
            if not isinstance(shifts_entry, dict):
                raise RuntimeError('full-size split plan shifts for a plate must be a mapping '
                                   f'of object id to [dx, dy, dz], got {shifts_entry!r}')
            plate_objects = {key_to_object[key] for key in keys}
            if set(shifts_entry) != plate_objects:
                raise RuntimeError('full-size split plan records shifts that do not name exactly '
                                   'the objects of their plate; refusing to slice it as authored')
            if len(keys) < 2:
                raise RuntimeError('full-size split plan records shifts for a plate that is not '
                                   'shared; refusing to slice it as authored')
            if placed:
                raise RuntimeError('full-size split plan records shifts for a plate that also '
                                   'records a placement; refusing to slice it as authored')
            # A turn or a reorientation on this plate is the shared form
            # validated above (#221 R2/R3): its members' records are realized
            # per object, composed with these shifts. (A solo turn or
            # reorientation with shifts was already refused above.)
            item_shifts = {}
            for oid, raw in shifts_entry.items():
                if not isinstance(raw, (list, tuple)) or len(raw) != 3:
                    raise RuntimeError(f'full-size split plan shift for object {oid} must be a '
                                       f'[dx, dy, dz] triple, got {raw!r}')
                try:
                    values = [float(v) for v in raw]
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(f'full-size split plan shift for object {oid} must be '
                                       f'numeric, got {raw!r}') from exc
                if not all(math.isfinite(v) for v in values):
                    raise RuntimeError(f'full-size split plan shift for object {oid} must be '
                                       f'finite, got {raw!r}')
                item_shifts[oid] = values
        fit_by_object = None
        member_rotations = {}
        if shared_yaw_by_object:
            fit_by_object = {}
            for oid, degrees in shared_yaw_by_object.items():
                rad = math.radians(degrees)
                c, s = math.cos(rad), math.sin(rad)
                fit_by_object[oid] = {'rotation': [c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0]}
                member_rotations[oid] = fit_by_object[oid]['rotation']
        if shared_reorientation_by_object:
            fit_by_object = fit_by_object or {}
            for oid, row in shared_reorientation_by_object.items():
                fit_by_object[oid] = {'rotation': row['rotation']}
                member_rotations[oid] = row['rotation']
        report = export_single_color_plate(src, plate, view, keys, bed=int(st['bed_mm'][0]),
                                           slot_colors=st.get('filament_slots') or {},
                                           **({'fit': fit} if fit is not None else {}),
                                           **({'fit_by_object': fit_by_object}
                                              if fit_by_object is not None else {}),
                                           **({'item_shifts': item_shifts} if item_shifts is not None else {}))
        if item_shifts is not None:
            # Re-read the derived plate: every object must have landed at
            # exactly the recorded shift, or the plate is not the proved
            # polygon placement and must not be sliced. A rotated member
            # (#221 R2 shared yaw, #221 R3 shared reorientation) must land at
            # its rotated authored bounds plus the shift, with the rotated
            # extents — a plate that rotated it by any other matrix, or not
            # at all, is not the recorded plan.
            from .color_geometry import _world_bounds as _bounds_of
            from .color_geometry import _world_points as _points_of
            from .orca_adapter import _rotate as _rotate_point
            with zipfile.ZipFile(src) as archive:
                authored = {oid: _bounds_of(archive, {oid})[0] for oid in item_shifts}
                rotated_points = {oid: _points_of(archive, {oid}) for oid in member_rotations}
            with zipfile.ZipFile(plate) as archive:
                landed_boxes = {oid: _bounds_of(archive, {oid})[0] for oid in item_shifts}
            for oid, values in item_shifts.items():
                if oid in member_rotations:
                    rotated = [_rotate_point(point, member_rotations[oid])
                               for point in rotated_points[oid]]
                    base_min = [min(point[a] for point in rotated) for a in range(3)]
                    base_extent = [max(point[a] for point in rotated) - base_min[a]
                                   for a in range(3)]
                    landed = landed_boxes[oid]
                    landed_extent = [landed[a][1] - landed[a][0] for a in range(3)]
                    if any(abs(landed[a][0] - (base_min[a] + values[a])) > 2e-3 for a in range(3)) \
                            or any(abs(got - want) > 2e-3
                                   for got, want in zip(landed_extent, base_extent)):
                        if shared_reorientation_by_object \
                                and oid in shared_reorientation_by_object:
                            raise RuntimeError('derived plate did not land at the recorded '
                                               'shared reorientation; refusing to slice '
                                               'it as reoriented')
                        raise RuntimeError('derived plate did not land at the recorded shared '
                                           'turn; refusing to slice it as turned')
                    continue
                for axis in range(3):
                    expected_min = authored[oid][axis][0] + values[axis]
                    if abs(landed_boxes[oid][axis][0] - expected_min) > 2e-3:
                        raise RuntimeError('derived plate did not land at the recorded shifts; '
                                           'refusing to slice it as placed')
        if placement_mm is not None:
            landed = report.get('placed_bounds_mm') or []
            if len(landed) != 2 or any(abs(landed[axis][0] - placement_mm[axis]) > 1e-6 for axis in range(2)):
                raise RuntimeError('derived plate did not land at the recorded placement; '
                                   'refusing to slice it as placed')
        if reorientation is not None:
            # Re-read the derived plate: the landed part must measure
            # exactly the rotated authored extents per axis and sit on the
            # bed (Z minimum ~0), or the plate is not the recorded
            # reorientation and must not be sliced.
            from .color_geometry import _world_bounds as _bounds_of
            from .color_geometry import _world_points as _points_of
            from .orca_adapter import _rotate as _rotate_point
            oid = next(iter(reoriented))
            with zipfile.ZipFile(src) as archive:
                authored_points = _points_of(archive, {oid})
            rotated = [_rotate_point(point, reorientation['rotation']) for point in authored_points]
            expected_extents = [max(p[a] for p in rotated) - min(p[a] for p in rotated)
                                 for a in range(3)]
            with zipfile.ZipFile(plate) as archive:
                landed_box = _bounds_of(archive, {oid})[0]
            landed_extents = [landed_box[a][1] - landed_box[a][0] for a in range(3)]
            if any(abs(landed - expected) > 2e-3
                   for landed, expected in zip(landed_extents, expected_extents)) \
                    or abs(landed_box[2][0]) > 2e-3:
                raise RuntimeError('derived plate did not land at the recorded reorientation; '
                                   'refusing to slice it as reoriented')
        slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
        out, provenance, configs = slice_model(plate, slice_dir, st, preserve_layout=True,
                                               filament_slot=slot,
                                               **({'supports': supports}
                                                  if supports is not None else {}))
        finalize_multicolor(out, None, st, {'0': slot})
        check = verify_gcode_offline(out, st)
        artifacts.append({'slot': slot, 'plate_id': index, 'binding_key': f'{index}:{slot}',
                          'part_keys': keys, 'plate': str(plate), 'gcode': str(out),
                          **({'yaw_deg': yaw_deg} if yaw_deg is not None else {}),
                          **({'yaw_by_object': shared_yaw_by_object}
                             if shared_yaw_by_object else {}),
                          **({'placement_mm': placement_mm} if placement_mm is not None else {}),
                          **({'reorientation': reorientation} if reorientation is not None else {}),
                          **({'reorientation_by_object': shared_reorientation_by_object}
                             if shared_reorientation_by_object else {}),
                          **({'shifts_by_object': item_shifts} if item_shifts is not None else {}),
                          'offline_check': check, 'plate_report': report, 'provenance': provenance,
                          'slice_config': [str(path) for path in configs]})
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest):
        raise RuntimeError('source, decisions or slice recipe changed during full-size split slicing')
    job['derived_plates'] = artifacts
    job['plate_merge'] = {'mode': 'split', 'reason': f"operator chose full-size split into {len(artifacts)} plates",
                          'plates_in': len(artifacts), 'parts_in': len(expected), 'fits_bed': True,
                          'bed_mm': int(st['bed_mm'][0]), 'full_size': True}
    job['artifact_binding'] = {**_recipe_binding(job), 'source_sha256': source_sha, 'decisions_sha256': decision_sha,
                               'gcode_sha256': {row['binding_key']: file_sha256(row['gcode']) for row in artifacts}}
    job.update({'gcode': artifacts[0]['gcode'], 'gcode_verified': True,
                'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
    write_job(work, job)
    return [row['gcode'] for row in artifacts]


def slice_job(job_path, brim_override=None, copies=None, scale=None, recipe_sha256=None):
    # Disk only. Launch still runs printer_state.check inside print_now and
    # verify_gcode. A file that never leaves disk does not need Moonraker or Spoolman.
    job = json.loads(Path(job_path).read_text()); st = printer_state.load()
    adapter = _job_adapter(job)
    # "generic" = not sliced through the record-bound IFS machinery (per-slot
    # selections validated against the record map, per-part plates exported by
    # record slot, native finalization). A multi_tool printer stays "generic"
    # here, and so does a Bambu AMS model (#671): multi_slot by class, but its
    # filament slots are the sliced file's own — Print Handy never slices per
    # slot for it — so its slicing behavior is exactly what it was as "single".
    generic = not (adapter.multi_slot and adapter.slots_bound_by_record)
    # A multi_tool printer stays "generic" for the IFS-shaped gates below, but
    # its per-toolhead colour decisions are honoured (#346): the refusal gate
    # and the decisions drop both exempt it, and per-part decisions route to
    # the identity tool-mapping slice.
    multi_tool = adapter.multi_tool
    job["printer_model"] = adapter.key  # print-now refuses a job sliced for another model
    _timing(job_path, 'slice_start')
    if copies is None:
        copies = job.get('copies')
    if copies:
        job['copies'] = int(copies)
    if not generic and job.get("filament_selection") is not None and (job.get('color_decisions') or {}).get('mode') != 'per_part':
        selection = resolve_filament_selection(st, job["filament_selection"])
        _validate_slot_selection(st, selection)
        job["filament_selection"] = selection
    src=Path(job["downloaded_file"]); ad=job["audit"]; multi_plate=False
    if src.suffix.lower() not in (".3mf",".stl"): raise RuntimeError("only STL/3MF can be sliced; selected source is not geometry")
    from . import slice_recipe as sr
    recipe = sr.record(sr.slice_recipe(job, file_sha256(src), st,
                                       copies=copies, scale=scale, brim_override=brim_override))
    if recipe_sha256 is not None and recipe_sha256 != recipe['sha256']:
        # The request was bound to a recipe the job no longer describes (a background slice
        # requested before the job changed). Slicing now would label one recipe with another.
        from .slice_failure import SliceFailure
        raise SliceFailure('slice request is for another slice recipe; the job changed after it was requested. '
                           'Nothing sliced', kind='recipe')
    # A Bambu/Orca multi-plate project is not one PrusaSlicer bed. Its aggregate
    # dimensions and center are not meaningful for an individual printable plate.
    if src.suffix.lower() == ".3mf":
        with zipfile.ZipFile(src) as archive:
            actual_plates = {int(match.group(1)) for name in archive.namelist()
                             if (match := re.fullmatch(r"Metadata/plate_(\d+)\.(?:png|gcode)", name))}
            if "Metadata/model_settings.config" in archive.namelist():
                try:
                    settings = ET.fromstring(archive.read("Metadata/model_settings.config"))
                    for plate in settings.iter("plate"):
                        for metadata in plate.iter("metadata"):
                            if metadata.get("key") == "plater_id":
                                plate_id = metadata.get("value")
                                if plate_id is None:
                                    raise RuntimeError("3MF plate assignment has no plater_id value")
                                actual_plates.add(int(plate_id))
                except (ET.ParseError, ValueError, TypeError) as exc:
                    raise RuntimeError(f"unable to inspect 3MF plate assignments: {exc}") from exc
        multi_plate = len(actual_plates) > 1 or int(ad.get("plates", 0)) > 1
    from .source_colors import inventory, prompts, require_single_color, validate_decisions
    try:
        manifest = inventory(src)
    except RuntimeError as exc:
        if multi_plate:
            raise RuntimeError(f'multi-plate 3MF cannot be merged or split: {exc}; no slicing attempted') from exc
        raise
    if generic and (multi_plate or manifest['requires_choices'] or job.get('full_size_split_plan')):
        # A multi_tool printer honours multi-colour and multi-plate sources
        # through the identity tool mapping (#346); a full-size split plan is
        # still IFS-shaped and stays refused here.
        if not multi_tool or job.get('full_size_split_plan'):
            raise MultiColorUnsupported(
                f"{adapter.label} has a single extruder; multi-plate, multi-part or multi-color sources need a "
                "multi-slot printer. Slice one single-color plate instead")
    # STL has no source palette; bind decisions to the exact file bytes, as intake does.
    if not manifest.get('source_sha256'):
        manifest['source_sha256'] = file_sha256(src)
    job['color_manifest'], job['color_prompts'] = manifest, prompts(manifest)
    write_job(Path(job_path).parent, job)
    _timing(job_path, 'inventory_done')
    job['slice_recipe'] = recipe  # persisted only with the artifact it describes
    decisions = None if (generic and not multi_tool) else job.get('color_decisions')
    if decisions is not None:
        decisions = validate_decisions(manifest, decisions, st, resolve_filament_selection)
    if manifest['requires_choices'] and not decisions:
        raise RuntimeError('part/color choices required; run color-plan and color-select before slicing')
    if copies and int(copies) > 1 and not generic and not copies_supported(job, multi_plate):
        # Split, multi-plate and per-part paths never clone copies (#37); keep JOB.json truthful.
        print(f'NOTE: {int(copies)} copies requested, but split and per-part jobs slice one copy per plate')
        copies = 1
        job['copies'] = 1
    if job.get('full_size_split_plan'):
        result = _slice_full_size_split(job, job_path, src, manifest, decisions, st)
        _timing(job_path, 'slice_done', path='full_size_split')
        return result
    if multi_plate:
        # Several source plates: merge onto one bed by default, otherwise one
        # derived plate per source plate. Never an unexplained refusal.
        try:
            result = slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override)
        except ImplicitPlateSplit as exc:
            if not adapter.multi_tool:
                raise
            raise RuntimeError(
                f"{adapter.label} prints a multi-color job as one merged plate; this source does not "
                f"merge onto one bed ({exc.verdict.get('reason')}). No slicing attempted") from exc
        _timing(job_path, 'slice_done', path='multi_plate')
        return result
    if decisions and decisions['mode'] == 'single_color':
        selection = decisions['selection']
        if not multi_tool:
            # The IFS slot verification is slot-shaped; a toolhead pick was
            # already resolved against the recorded toolheads (#346).
            _validate_slot_selection(st, selection)
        job['filament_selection'] = selection
    elif decisions and decisions['mode'] == 'per_part':
        if adapter.multi_tool:
            # Independent toolheads (#346): one merged G-code through the
            # identity tool mapping, or a stated refusal — never the IFS
            # per-slot split below, whose plates finalize through a native
            # slot contract this printer does not have.
            try:
                return slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override)
            except ImplicitPlateSplit as exc:
                raise RuntimeError(
                    f"{adapter.label} prints a multi-color job as one merged plate; this source does not "
                    f"merge onto one bed ({exc.verdict.get('reason')}). No slicing attempted") from exc
        if src.suffix.lower() == '.3mf':
            # Single-plate per-part decisions use the same merge planner as
            # multi-plate sources: one multicolor G-code unless a stated reason splits.
            try:
                return slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override)
            except ImplicitPlateSplit as exc:
                print('SPLIT single-plate 3MF into per-slot G-code: ' + str(exc.verdict.get('reason')))
                job['plate_merge'] = exc.verdict
        # Non-mergeable and non-3MF per-part sources use the single-slot plate split.
        groups = single_color_groups(job, manifest)
        if not groups:
            raise RuntimeError('per-part decision did not resolve any single-color plate')
        # Every slot group is sliced. The loaded spool is a launch fact, not a
        # reason to drop a plate that already names its own slot.
        from .color_geometry import export_single_color_plate
        from .orca_adapter import slice_model
        import uuid
        work = Path(job_path).parent
        derived = work / 'derived'
        derived.mkdir(exist_ok=True)
        source_sha = file_sha256(src)
        decision_sha = decisions_digest(decisions)
        fit, supports, brim_override = _job_fit(job, brim_override)
        artifacts = []
        for group in groups:
            plate = derived / f"slot{group['slot']}.3mf"
            placement_kw = {'fit': fit} if fit is not None else {}
            if fit is None and job.get('full_size_rearrange_plan'):
                # A chosen 100% rearrangement that could not merge onto one
                # multicolor bed: each slot plate is packed on its own bed
                # (parts may turn 90 degrees) instead of keeping the
                # author's layout, which is what overflowed the bed.
                placement_kw = _rearranged_group_placement(src, manifest, group, int(st['bed_mm'][0]),
                                                           brim=brim_override)
            report = export_single_color_plate(src, plate, decisions, group['part_keys'], bed=int(st['bed_mm'][0]), slot_colors=st.get('filament_slots') or {}, **placement_kw)
            slice_dir = work / 'sliced' / 'orca' / uuid.uuid4().hex
            out, provenance, configs = slice_model(plate, slice_dir, st, brim_override, preserve_layout=True, filament_slot=group['slot'], **({'supports': supports} if supports is not None else {}))
            # Keep the plate's physical IFS slot explicit. Orca may retain
            # project-wide unused filament_type entries although all geometry
            # is remapped to logical T0 on this single-color derivative.
            material = st.get('filament_slots', {}).get(str(group['slot']), {}).get('material')
            if not material:
                raise RuntimeError(f"derived plate physical IFS slot {group['slot']} has no verified material")
            finalize_multicolor(out, None, st, {'0': group['slot']})
            # Verify the exact final bytes (after map, color and MD5 rewrite).
            check = verify_gcode_offline(out, st)
            artifacts.append({'slot': group['slot'], 'part_keys': group['part_keys'], 'plate': str(plate),
                              'gcode': str(out), 'offline_check': check, 'plate_report': report,
                              'provenance': provenance, 'slice_config': [str(path) for path in configs]})
        latest = json.loads(Path(job_path).read_text())
        if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest):
            raise RuntimeError('source, decisions or slice recipe changed during slicing; artifacts not made ready')
        job['derived_plates'] = artifacts
        job.pop('refused_plates', None)
        job['artifact_binding'] = {**_recipe_binding(job), 'source_sha256': source_sha, 'decisions_sha256': decision_sha,
                                   'gcode_sha256': {row['slot']: file_sha256(row['gcode']) for row in artifacts}}
        job.update({'gcode': artifacts[0]['gcode'], 'gcode_verified': True,
                    'sliced_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')})
        _timing(job_path, 'slice_done')
        write_job(work, job)
        print('SLICED separate single-color plates (not uploaded): ' + ', '.join(row['gcode'] for row in artifacts))
        return [row['gcode'] for row in artifacts]
    scale = scale if scale is not None else job.get("scale")
    explicit_scale = scale is not None
    if scale is not None:
        scale = float(scale)
        if not scale > 0:
            raise RuntimeError('scale must be greater than 0')
    orientation = job.get("orientation") or {}
    rotation = orientation.get("rotation")
    if not all(ad.get(k) for k in ("size_x", "size_y", "size_z")):
        # Jobs audited by the retired Prusa probe can lack extents; re-measure.
        for line in model_info_output(src).splitlines():
            k, _, v = line.partition(" = ")
            if k in ("size_x", "size_y", "size_z"): ad[k] = v
    sx,sy,sz=(float(ad[k]) for k in ("size_x","size_y","size_z"))
    authored_dims = (sx, sy, sz)
    if rotation and orientation.get("dims_full_mm"):
        # Operator chose a rotated orientation: check the rotated envelope, not the authored one.
        sx, sy, sz = (float(v) for v in orientation["dims_full_mm"])
    rearrange_plan = job.get("full_size_rearrange_plan") or {}
    if rearrange_plan.get("size_mm"):
        sx, sy, sz = (float(v) for v in rearrange_plan["size_mm"])
    auto_scale = None
    unit_provenance = job.get('unit_provenance') if isinstance(job.get('unit_provenance'), dict) else None
    if src.suffix.lower() == '.stl':
        if explicit_scale:
            # An explicit scale — 1.0 included — is the operator's own size
            # instruction; it settles the size and is never overridden.
            unit_provenance = {'units': None, 'source': 'explicit_scale',
                               'original_mm': list(authored_dims), 'factor': float(scale)}
        elif _is_generated_mm(job):
            unit_provenance = {'units': 'mm', 'source': 'generated',
                               'original_mm': list(authored_dims), 'factor': 1.0}
        elif max(authored_dims) < UNIT_QUESTION_MAX_MM:
            # STL carries no units and a sub-10 mm part may be authored in
            # millimeters or inches. Never assume inches: slicing waits for a
            # source-bound user confirmation recorded on the job (#206).
            decision = job.get('unit_decision')
            expected_sha = manifest.get('source_sha256') or file_sha256(src)
            if not isinstance(decision, dict) or decision.get('source_sha256') != expected_sha:
                measured = ' x '.join(f'{value:g}' for value in authored_dims)
                as_inch = ' x '.join(f'{value * INCH_TO_MM:g}' for value in authored_dims)
                raise RuntimeError(
                    f'model measures {measured} mm and STL files do not record units; confirm whether that '
                    f'size is millimeters (prints at {measured} mm) or inches (prints at {as_inch} mm) before '
                    'slicing — record the answer on this job (unit-select JOB.json --units mm|inch, or slice '
                    '--units mm|inch). Nothing sliced')
            try:
                confirmed = normalize_units(decision.get('units'))
            except ValueError:
                raise RuntimeError('recorded unit confirmation must be mm or inch; confirm the units again '
                                   'before slicing. Nothing sliced')
            if confirmed == 'inch':
                inches_scale = INCH_TO_MM
                inch_dims = tuple(round(value * inches_scale, 6) for value in (sx, sy, sz))
                if (inch_dims[0] > st["bed_mm"][0] or inch_dims[1] > st["bed_mm"][1]
                        or inch_dims[2] > st["max_print_height_mm"]):
                    measured = f'{sx:g}x{sy:g}x{sz:g}'
                    final = f'{inch_dims[0]:g}x{inch_dims[1]:g}x{inch_dims[2]:g}'
                    raise RuntimeError(f'model measures {measured} mm and treating it as inches would be {final} mm, which exceeds printer volume')
                scale = inches_scale
                auto_scale = {'factor': inches_scale, 'reason': 'inch units',
                              'original_mm': [sx, sy, sz], 'final_mm': list(inch_dims)}
                unit_provenance = {'units': 'inch', 'source': 'user_confirmation',
                                   'original_mm': list(authored_dims), 'factor': inches_scale}
            else:
                unit_provenance = {'units': 'mm', 'source': 'user_confirmation',
                                   'original_mm': list(authored_dims), 'factor': 1.0}
        else:
            unit_provenance = {'units': 'mm', 'source': 'authored_size',
                               'original_mm': list(authored_dims), 'factor': 1.0}
    eff_sx, eff_sy, eff_sz = sx * (float(scale) if scale else 1.0), sy * (float(scale) if scale else 1.0), sz * (float(scale) if scale else 1.0)
    if eff_sx>st["bed_mm"][0] or eff_sy>st["bed_mm"][1] or eff_sz>st["max_print_height_mm"]: raise RuntimeError(f"model {sx}x{sy}x{sz} mm exceeds printer volume")
    if brim_override is None and job.get('fit_brim_policy'):
        brim_override = job['fit_brim_policy'].get('width_mm', 0)
    if rotation and brim_override is None and orientation.get("brim"):
        brim_override = 5
    # Canonical slicing is Orca only.
    from .orca_adapter import slice_model
    work = Path(job_path).parent
    import uuid
    # Preserve historical artifact bytes on every reslice.
    slice_dir = work / "sliced" / "orca" / uuid.uuid4().hex
    source_sha = file_sha256(src)
    slice_src = src
    if rearrange_plan:
        from .geometry_layout import arrange_and_export
        rearrange_dir = slice_dir / "rearranged_100pct"
        from .geometry_layout import DEFAULT_MARGIN_MM as _arrange_edge
        layout_report = arrange_and_export(
            src,
            rearrange_dir,
            bed=(float(st["bed_mm"][0]), float(st["bed_mm"][1]), float(st["max_print_height_mm"])),
            # layout.arrange_edge_mm plus the job's brim, so the whole brim stays on the bed.
            margin=_arrange_edge + float(brim_override or 0),
            allow_geometry_only_strip=not bool(decisions and decisions.get("mode") in ("multicolor", "per_part")),
        )
        preserved = (layout_report.get("preserved_3mf_package") or {}).get("preserved_3mf")
        slice_src = rearrange_dir / (preserved or layout_report["exported_3mf_geometry_only"])
        job["layout_report"] = str(rearrange_dir / "validation.json")
    # A single-slot printer slices as `generic`, which leaves `decisions` as None, but the Discord
    # intake has still written color_decisions into JOB.json. The post-slice check below hashes
    # the file's decisions, so hash the same thing here or every such slice is refused (#268).
    decision_sha = decisions_digest(decisions if not generic else job.get("color_decisions"))
    selected_slot = None if generic else (job.get("filament_selection") or {}).get("slot")
    scale_kw = {'scale': scale} if scale is not None else {}
    if adapter.key != printers.current().key:
        scale_kw['adapter'] = adapter
    if rotation:
        scale_kw.update(rotation=rotation, supports=bool(orientation.get("supports")))
    _timing(job_path, 'orca_begin')
    if selected_slot is not None:
        # Single-color jobs are bound to their named IFS slot exactly like
        # derived per-slot plates, so launch uses the mapped native route.
        out, provenance, configs = slice_model(slice_src, slice_dir, st, brim_override,
                                              color_decisions=decisions, filament_slot=selected_slot,
                                              copies=copies, **scale_kw)
        finalize_multicolor(out, None, st, {"0": selected_slot})
    else:
        out, provenance, configs = slice_model(slice_src, slice_dir, st, brim_override,
                                              color_decisions=decisions, copies=copies, **scale_kw)
    _timing(job_path, 'orca_finalize_done')
    if adapter.key == printers.current().key:
        verify_gcode(out)
    latest = json.loads(Path(job_path).read_text())
    _timing(job_path, 'verify_done')
    if file_sha256(src) != source_sha or decisions_digest(latest.get("color_decisions")) != decision_sha or _recipe_changed(job, latest):
        raise RuntimeError("source, decisions or slice recipe changed during slicing; artifact not made ready")
    job["artifact_binding"] = {**_recipe_binding(job), "source_sha256": source_sha, "decisions_sha256": decision_sha,
                               "gcode_sha256": file_sha256(out)}
    if auto_scale is not None:
        job['auto_scale'] = auto_scale
    else:
        job.pop('auto_scale', None)
    if unit_provenance is not None:
        job['unit_provenance'] = unit_provenance
    else:
        job.pop('unit_provenance', None)
    job.update({"printer_model": adapter.key, "slice_config": [str(p) for p in configs], "gcode": str(out),
                "provenance": provenance, "gcode_verified": True,
                # Only this path clones copies into the G-code; estimates read it, never the request.
                "sliced_copies": {"gcode": str(out), "copies": max(1, int(copies or 1))},
                "sliced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    write_job(work, job)
    print(f"SLICED (not uploaded): {out}")
    _timing(job_path, 'slice_done')
    return out


def prepare_first_layer(slot, color, material, spoolman_id, slicer="orca"):
    """Offline geometry + real slicer output. No printer contact or start permission."""
    st = printer_state.load()
    selection = {"slot": slot, "color": color, "material": material, "spoolman_id": spoolman_id}
    mapped = st.get("filament_slots", {}).get(str(slot), {})
    if (mapped.get("spoolman_id") != spoolman_id or
            str(mapped.get("color", "")).casefold() != color.casefold() or
            str(mapped.get("material", "")).casefold() != material.casefold() or
            st["spool"]["spoolman_id"] != spoolman_id):
        raise ValueError("selected filament differs from recorded slot/active spool; live reconciliation required")
    folder = ARCHIVE / "ad5x-first-layer-sliced"
    source = folder / "source" / "first_layer_50x50.stl"
    source.parent.mkdir(parents=True, exist_ok=True)
    # Closed 50 x 50 x 0.2 mm rectangular solid, one printable layer.
    vertices = [(0,0,0), (50,0,0), (50,50,0), (0,50,0),
                (0,0,0.2), (50,0,0.2), (50,50,0.2), (0,50,0.2)]
    faces = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),
             (1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    lines = ["solid first_layer"]
    for face in faces:
        lines += ["facet normal 0 0 0", "outer loop"]
        lines += ["vertex " + " ".join(map(str, vertices[i])) for i in face]
        lines += ["endloop", "endfacet"]
    source.write_text("\n".join(lines + ["endsolid first_layer"]) + "\n")
    from .orca_adapter import slice_model
    if slicer != "orca":
        raise ValueError("only Orca slicing is supported; legacy Prusa is retired")
    output, provenance, configs = slice_model(source, folder / "sliced", st, first_layer=True)
    text = output.read_text()
    if text.count(";LAYER_CHANGE") != 1:
        raise RuntimeError("first-layer output must contain exactly one layer")
    config_desc = [str(p) for p in configs]
    command = provenance["slicer_command"]
    job = {"title": "AD5X First Layer Test Square 50x50mm", "slug": folder.name,
           "downloaded_file": str(source), "gcode": str(output), "slice_config": config_desc,
           "filament_selection": selection, "gcode_verified": False,
           "audit": {"size_x": 50, "size_y": 50, "size_z": 0.2, "parts": 1},
           "provenance": provenance,
           "next_action": "Live readiness and verify-gcode required before authorized print-now"}
    stamp_job_printer(job)
    write_job(folder, job)
    print("PREPARED OFFLINE (not verified live, not uploaded): " + str(output))
    return output


def _filament_total_g(text):
    """Total filament grams from the slicer summary comments, or None when absent.

    OrcaSlicer/Flash spell it "; total filament used [g] = X"; Bambu Studio's
    header block spells it "; total filament weight [g] : X" (#444, verified
    against a real 2.8.2.61 slice). Absent stays None (unknown), never zero.
    """
    m = (re.search(r"^; total filament used \[g\] = ([\d.]+)", text or "", re.M)
         or re.search(r"^;\s*total filament weight \[g\]\s*:\s*([\d.]+)", text or "", re.M))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _bambu_total_estimated_time(text):
    """The header-block "total estimated time" string, for Bambu files only (#444).

    Both BBL slicers write that header totals line with the same spelling, so
    this is gated on the BambuStudio identity line and OrcaSlicer files keep
    exactly the metadata they had before.
    """
    if not re.search(r"^; BambuStudio ", text or "", re.M):
        return None
    m = re.search(r"^;.*?total estimated time\s*:\s*([^;\n]+)", text or "", re.M)
    return m.group(1).strip() if m else None


def gcode_estimate_s(text):
    m=re.search(r'^; estimated printing time \(normal mode\) = (.+)$',text,re.M)
    if not m:
        # Bambu Studio (and Orca's header block) write "; model printing time: 1h 2m 3s; total estimated
        # time: 1h 7m 3s" instead (#271): printing time first, the slicer's total as the fallback.
        m=(re.search(r'^;.*?\bmodel printing time\s*:\s*([^;\n]+)',text,re.M)
           or re.search(r'^;.*?\btotal estimated time\s*:\s*([^;\n]+)',text,re.M))
    if not m: return None
    return sum(int(n)*{"d":86400,"h":3600,"m":60,"s":1}[u] for n,u in re.findall(r'(\d+)\s*([dhms])',m.group(1))) or None


def human_dur(sec):
    h,m=divmod(int(round(sec/60)),60)
    return f"{h}h {m}m" if h else f"{m}m"


def _mr(st, path, data=None, timeout=30):
    return request_json(st["moonraker_url"]+path, data=data, timeout=timeout,
                        headers={"Content-Type":"application/json"} if data is not None else None)


def remote_name(gcode, sha, job=None):
    """Moonraker file name: readable model title first, then the plate stem and exact SHA256.

    The title makes Moonraker, Fluidd and the printer screen show what is printing; the
    stem plus full hash keeps the name unique and lets discord_print_summary.find_job
    bind it back to the exact local artifact.
    """
    g = Path(gcode)
    title = job.get("title") if isinstance(job, dict) else None
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(title or "")).strip("-")[:40].strip("-")
    stem = g.stem if not slug or slug.lower() == g.stem.lower() else f"{slug}_{g.stem}"
    return f"{stem}_{sha}{g.suffix}"


def _zmod_version(st):
    """Installed ZMod version tuple from Moonraker's update status, or None (#564).

    Fail-soft by design: any transport or parse problem is "unknown", never an
    error — auto format resolution treats unknown as plain .gcode.
    """
    try:
        status = firmware_update.fetch_status(lambda _method, path: _mr(st, path))
    except Exception:
        return None
    return gcode_3mf.version_from_status(status)


def _note_delivery_format(job, job_path, note):
    """Persist the resolved delivery format on the job so record_start lifts it
    into the print record (#564). Best effort: a failed note never blocks a print."""
    if not isinstance(job, dict) or not job_path:
        return
    try:
        job["delivery_format"] = note
        write_job(Path(job_path).parent, job)
    except Exception as exc:
        logger.warning("could not record delivery format: %s", exc)


def _zmod_upload_artifact(adapter, st, g, sha, remote):
    """Resolve the Moonraker upload artifact for a Flashforge/ZMod print (#564).

    Returns (upload_path, upload_sha256, remote, note). The default — and every
    fallback — is the plain finalized .gcode exactly as before. When the
    printer's zmod_delivery_format resolves to 3mf, the finalized G-code is
    packaged as .gcode.3mf next to itself: the upload checksum becomes the
    PACKAGE's sha256 (Moonraker verifies it against the uploaded bytes), while
    the remote name keeps the G-code's stem+sha with a .3mf suffix so
    find_job/record binding is unchanged. All verification has already run on
    the .gcode by the time this is called; nothing here re-checks or alters it.
    Callers note the returned note on the job once the upload verifies.
    """
    setting = str(adapter.config.get("printer", {}).get("zmod_delivery_format") or "gcode").strip().casefold()
    if getattr(getattr(adapter, "model", None), "vendor_key", None) != "flashforge":
        return g, sha, remote, None
    version = _zmod_version(st) if setting == "auto" else None
    fmt, reason = gcode_3mf.resolve_format(setting, version)
    if fmt != "3mf":
        return g, sha, remote, None
    nozzle = (st.get("nozzle") or {}).get("diameter_mm")
    package = gcode_3mf.package(g, gcode_3mf.package_path_for(g),
                                nozzle_mm=float(nozzle) if isinstance(nozzle, (int, float)) else None)
    note = {"format": "3mf", "setting": setting, "reason": reason,
            "zmod_version": ".".join(map(str, version)) if version else None,
            "package_sha256": file_sha256(package)}
    packaged_remote = remote[:-len(".gcode")] + ".gcode.3mf" if remote.endswith(".gcode") else remote + ".3mf"
    print(f"DELIVERY FORMAT: .gcode.3mf ({reason})", flush=True)
    return package, note["package_sha256"], packaged_remote, note


def _announce_print_name(st, job, slots=None, gcode=None):
    """Best effort: publish "<title> (<colors>)" as Klipper display_status.message.

    The Moonraker Home Assistant integration already exposes that field as the
    "Current Display Message" sensor, so no listener or HA change is needed.
    Sent before the start command because a script queued during START_PRINT
    heating would block on the gcode mutex. Never blocks or fails a print.
    """
    title = job.get("title") if isinstance(job, dict) else None
    if not title:
        title = Path(str((job or {}).get("downloaded_file") or "")).stem if isinstance(job, dict) else ""
    name = re.sub(r"[^\w .,&+#@:/-]", "", str(title or "")).strip()[:50]
    if not name:
        return None
    colors = []
    for slot in slots or []:
        row = (st.get("filament_slots") or {}).get(str(slot)) or {}
        label = " ".join(w.capitalize() for w in str(row.get("color") or "").split())
        if label and label not in colors:
            colors.append(label)
    if not colors and isinstance(job, dict):
        sel = job.get("filament_selection") or {}
        if sel.get("color"):
            colors.append(" ".join(w.capitalize() for w in str(sel["color"]).split()))
    plates = (job.get("derived_plates") or []) if isinstance(job, dict) else []
    if gcode is not None and len(plates) > 1:
        paths = [str(r.get("gcode") or "") for r in plates]
        if str(gcode) in paths:
            name = f"{name} {paths.index(str(gcode)) + 1}/{len(plates)}"
    if colors:
        name = f"{name} ({' + '.join(colors)})"
    try:
        request_json(st["moonraker_url"] + "/printer/gcode/script",
                     data={"script": f'SET_DISPLAY_TEXT MSG="{name}"'}, timeout=5,
                     headers={"Content-Type": "application/json"})
        print("PRINT NAME PUBLISHED " + json.dumps({"name": name}), flush=True)
        return name
    except Exception as exc:
        print(f"WARNING: print name not published: {exc}", flush=True)
        return None


def _usage_for_source_extruder(manifest, source_extruder):
    stats = (manifest or {}).get('filament_stats') or {}
    extruders = stats.get('extruders') if isinstance(stats, dict) else {}
    if not isinstance(extruders, dict):
        return None
    row = extruders.get(source_extruder, extruders.get(str(source_extruder)))
    grams = row.get('g') if isinstance(row, dict) else None
    return float(grams) if isinstance(grams, (int, float)) and not isinstance(grams, bool) and grams > 0 else None


def source_site_estimate(row):
    """(seconds, grams) summed over the author's plates from the model page (MakerWorld), else None."""
    plates = (row or {}).get('plates') if isinstance(row, dict) else None
    if not isinstance(plates, list) or not plates:
        return None
    seconds = grams = 0.0
    for plate in plates:
        if not isinstance(plate, dict):
            return None
        s, g = plate.get('prediction_s'), plate.get('weight_g')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < 1e7 for v in (s, g)):
            return None
        seconds += float(s)
        grams += float(g)
    return seconds, grams


def _reference_manifest(job, manifest):
    """Replace a volume-guessed total with the model page's own weight when one exists.

    A guessed total also lands entirely on extruder 1, so every part shares it equally.
    """
    stats = (manifest or {}).get('filament_stats') or {}
    parts = [p for p in (manifest or {}).get('parts') or [] if isinstance(p, dict)]
    extruders = stats.get('extruders') if isinstance(stats.get('extruders'), dict) else {}
    used = {str(p.get('source_extruder')) for p in parts if p.get('source_extruder') is not None}
    guessed = bool(stats.get('estimated')) or bool(used - {str(k) for k in extruders})
    if not guessed:
        return manifest, False
    site = source_site_estimate((job or {}).get('selected_source'))
    total = site[1] if site else stats.get('total_g')
    if not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0:
        return manifest, False
    flat = {**manifest, 'parts': [{**p, 'source_extruder': 1} for p in parts],
            'filament_stats': {**stats, 'total_g': float(total), 'extruders': {'1': {'g': float(total)}}}}
    return flat, True


def filament_usage_by_selection(job, manifest=None):
    """Aggregate source estimates by confirmed selection, without duplicating multi-color totals."""
    manifest = manifest or (job or {}).get('color_manifest') or {}
    manifest, _ = _reference_manifest(job, manifest)
    decisions = (job or {}).get('color_decisions') or {}
    if decisions.get('mode') == 'single_color':
        selection = decisions.get('selection') or (job or {}).get('filament_selection')
        total = ((manifest.get('filament_stats') or {}).get('total_g')
                 if isinstance(manifest, dict) else None)
        if isinstance(selection, dict) and isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
            return [(selection, float(total))]
        return []
    choices = decisions.get('choices') if isinstance(decisions, dict) else None
    if not isinstance(choices, dict):
        return []
    # Painted sources contain both geometry and palette choice rows. The palette
    # rows are the authoritative per-extruder allocation, so do not count both.
    palette_choices = {key: value for key, value in choices.items()
                       if isinstance(key, str) and key.startswith('filament:')}
    part_extruders = {'part:' + str(part.get('key')): part.get('source_extruder')
                      for part in manifest.get('parts') or [] if isinstance(part, dict)}
    choice_rows = palette_choices or choices
    # An extruder total belongs to every part on that extruder together, never to each one.
    # Split it across those parts (by volume when known) so N parts never count N x the total.
    sharers = {}
    for key in choice_rows:
        if isinstance(key, str) and not key.startswith('filament:'):
            sharers.setdefault(str(part_extruders.get(key)), []).append(key)
    volumes = {'part:' + str(p.get('key')): p.get('volume_mm3')
               for p in manifest.get('parts') or [] if isinstance(p, dict)}
    totals = {}
    for key, selection in choice_rows.items():
        if not isinstance(selection, dict) or not isinstance(key, str):
            continue
        source_text = (key.split(':', 1)[1] if key.startswith('filament:') else part_extruders.get(key))
        try:
            source_extruder = int(source_text)
        except (TypeError, ValueError):
            continue
        grams = _usage_for_source_extruder(manifest, source_extruder)
        if grams is None:
            continue
        if not key.startswith('filament:'):
            group = sharers.get(str(source_text)) or [key]
            vols = [volumes.get(k) for k in group]
            if all(isinstance(v, (int, float)) and v > 0 for v in vols):
                grams *= float(volumes[key]) / sum(vols)
            else:
                grams /= len(group)
        identity = (selection.get('slot'), selection.get('spoolman_id'),
                    str(selection.get('color') or '').casefold(), str(selection.get('material') or '').casefold())
        item = totals.setdefault(identity, [selection, 0.0])
        item[1] += grams
    return [(selection, grams) for selection, grams in totals.values()]


def intake_spool_projection(job, spool_weights, manifest=None, copies=1):
    """Remaining-weight projection from exact source estimates and saved choices."""
    try:
        copies = max(1, int(copies or 1))
    except (TypeError, ValueError):
        copies = 1
    rows = []
    for selection, grams in filament_usage_by_selection(job, manifest):
        grams *= copies
        remaining = spool_weights.get(selection.get('spoolman_id')) if isinstance(spool_weights, dict) else None
        projected = (float(remaining) - grams if isinstance(remaining, (int, float))
                     and not isinstance(remaining, bool) else None)
        rows.append({'slot': selection.get('slot'), 'color': selection.get('color'),
                     'material': selection.get('material'), 'spoolman_id': selection.get('spoolman_id'),
                     'estimated_used_g': round(grams, 1),
                     'remaining_g': round(float(remaining), 1) if isinstance(remaining, (int, float))
                     and not isinstance(remaining, bool) else None,
                     'projected_remaining_g': round(projected, 1) if projected is not None else None})
    return rows


def intake_spool_weights(state, spool_ids, fetch=None):
    """Currently available grams per Spoolman ID: Spoolman first, then the slot record, else absent.

    Display only. Never raises; an unreachable Spoolman just falls back.
    """
    slots = {s.get('spoolman_id'): s for s in ((state or {}).get('filament_slots') or {}).values()
             if isinstance(s, dict) and s.get('spoolman_id') is not None}
    weights = {}
    for sid in spool_ids:
        grams = None
        if fetch is not None and isinstance(sid, int) and not isinstance(sid, bool):
            try:
                rem = (fetch(sid) or {}).get('remaining_weight')
                if isinstance(rem, (int, float)) and not isinstance(rem, bool) and rem >= 0:
                    grams = float(rem)
            except Exception:
                grams = None
        if grams is None:
            rem = (slots.get(sid) or {}).get('remaining_grams')
            if isinstance(rem, (int, float)) and not isinstance(rem, bool) and rem >= 0:
                grams = float(rem)
        if grams is not None:
            weights[sid] = grams
    return weights


def format_spool_projection(rows, position_noun='Slot'):
    """One short line per spool: needed, available, left after. Unknowns stay unknown."""
    lines = []
    for r in rows:
        label = ' '.join(str(x) for x in (r.get('color'), r.get('material')) if x) or 'filament'
        if r.get('slot') is not None:
            label = f"{position_noun} {r['slot']} {label}"
        line = f"{label}: needs about {r['estimated_used_g']:g} g"
        if r.get('remaining_g') is None:
            line += '; spool weight unknown.'
        elif r['projected_remaining_g'] < 0:
            line += (f"; spool has {r['remaining_g']:g} g, short by about "
                     f"{-r['projected_remaining_g']:g} g.")
        else:
            line += (f"; spool has {r['remaining_g']:g} g, about "
                     f"{r['projected_remaining_g']:g} g left after.")
        lines.append(line)
    return '\n'.join(lines)


def job_card(job, remote, gcode_text=""):
    """Copies and spools for the Home Assistant print card: one JSON document, no secrets."""
    job = job if isinstance(job, dict) else {}
    dec = job.get("color_decisions") or {}
    picks = [dec["selection"]] if isinstance(dec.get("selection"), dict) else list((dec.get("choices") or {}).values())
    if not picks and isinstance(job.get("filament_selection"), dict):
        picks = [job["filament_selection"]]
    spools, seen = [], set()
    for row in picks:
        if isinstance(row, dict) and row.get("slot") not in seen:
            seen.add(row.get("slot"))
            spools.append({k: row.get(k) for k in ("slot", "color", "material", "spoolman_id")})
    grams = _filament_total_g(gcode_text)
    return {"title": job.get("title") or Path(str(remote)).stem, "file": remote,
            "copies": int(job.get("copies") or 1), "spools": spools,
            "filament_g": round(grams, 1) if grams is not None else None,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def _publish_job_card(st, job, remote, gcode_text=""):
    """Best effort: Moonraker database hermes/current_job, read by Home Assistant. Never blocks a print."""
    try:
        card = job_card(job, remote, gcode_text)
        request_json(st["moonraker_url"] + "/server/database/item",
                     data={"namespace": "hermes", "key": "current_job", "value": card}, timeout=5,
                     headers={"Content-Type": "application/json"})
        return card
    except Exception as exc:
        print(f"WARNING: job card not published: {exc}", flush=True)
        return None


def _fetch_remote_gcode_sha256(moonraker_url, remote_name, timeout=60):
    escaped = urllib.parse.quote(remote_name)
    url = f"{moonraker_url}/server/files/gcodes/{escaped}"
    hasher = hashlib.sha256()
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total_read = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            total_read += len(chunk)
            if total_read > 1024 * 1024 * 1024:
                raise RuntimeError(f"Remote file {remote_name} exceeds 1GB limit")
    return hasher.hexdigest()


def _printer_phase(st):
    """Return a single adapter's reported phase; native 'printing' needs corroboration."""
    native=_native()
    q=_mr(st,"/printer/objects/query?"+("save_variables&" if native else "")+"print_stats&idle_timeout&virtual_sdcard&webhooks")["result"]["status"]
    variables=(q.get("save_variables") or {}).get("variables") or {}
    mode=variables.get("display_off")
    if native and mode not in (0,1):
        raise RuntimeError(f"live display_off mode is {mode!r}; refusing ambiguous hardware behavior")
    st["display_off"]=mode
    st["force_md5"]=variables.get("force_md5")
    stats=q.get("print_stats") or {}
    state=stats.get("state")
    filename=stats.get("filename")
    idle=(q.get("idle_timeout") or {}).get("state")
    virtual_sd = q.get("virtual_sdcard") or {}
    webhooks = q.get("webhooks") or {}
    # print_stats can remain standby/empty while PRINT_START is heating or
    # otherwise preparing. These independent Klipper signals still prove the
    # submitted job has been accepted; do not infer failure or retry it.
    accepted = (virtual_sd.get("is_active") is True or bool(virtual_sd.get("file_path"))
                or webhooks.get("state") == "Printing" or idle == "Printing")
    if state not in ("standby", "printing", "paused", "complete", "error", "cancelled"):
        raise RuntimeError(f"Moonraker print state unknown: {state!r}")
    if state in ("printing", "paused"):
        return state, filename, "Moonraker reports active execution"
    return state, filename, "Printing" if accepted else idle


class StartReadback(NamedTuple):
    outcome: str
    evidence: Any
    message: str


def classify_start_readback(status_snapshot) -> StartReadback:
    """Pure function classifying printer status after submission.

    Outcomes:
    - 'preparing': accepted, with heating, homing or prep evidence.
    - 'printing': state printing.
    - 'failed': error or cancelled.
    - 'unconfirmed': no evidence.
    """
    if not isinstance(status_snapshot, dict):
        status = {}
    elif "result" in status_snapshot and isinstance(status_snapshot["result"], dict) and "status" in status_snapshot["result"]:
        status = status_snapshot["result"]["status"]
    elif "status" in status_snapshot and isinstance(status_snapshot["status"], dict):
        status = status_snapshot["status"]
    else:
        status = status_snapshot

    ps = status.get("print_stats") if isinstance(status.get("print_stats"), dict) else {}
    ext = status.get("extruder") if isinstance(status.get("extruder"), dict) else {}
    bed = status.get("heater_bed") if isinstance(status.get("heater_bed"), dict) else {}
    vsd = status.get("virtual_sdcard") if isinstance(status.get("virtual_sdcard"), dict) else {}
    idle = status.get("idle_timeout") if isinstance(status.get("idle_timeout"), dict) else {}
    hooks = status.get("webhooks") if isinstance(status.get("webhooks"), dict) else {}
    toolhead = status.get("toolhead") if isinstance(status.get("toolhead"), dict) else {}

    raw_state = ps.get("state") or status.get("state")
    state = raw_state.lower() if isinstance(raw_state, str) else None

    # 1. Failed
    hook_state = (hooks.get("state") or "").lower() if isinstance(hooks.get("state"), str) else ""
    if state in ("error", "cancelled") or hook_state in ("error", "shutdown"):
        fail_state = state or hook_state or "error"
        return StartReadback(
            outcome="failed",
            evidence={"state": fail_state},
            message=f"print failed: state is {fail_state}",
        )

    # 2. Printing
    if state == "printing":
        filament_used = None
        for k in ("filament_used", "filament_used_mm", "filament_length"):
            if k in ps:
                filament_used = ps[k]
                break
            if k in status:
                filament_used = status[k]
                break

        extruder_delta = None
        for candidate in (
            status.get("extruder_position_delta"),
            status.get("extruder_delta"),
            ext.get("position_delta"),
            ext.get("delta"),
            toolhead.get("extruder_position_delta"),
            toolhead.get("extruder_delta"),
        ):
            if candidate is not None:
                extruder_delta = candidate
                break

        extrusion_confirmed = False
        if filament_used is not None:
            try:
                if float(filament_used) > 0:
                    extrusion_confirmed = True
            except (ValueError, TypeError):
                pass
        if extruder_delta is not None:
            try:
                if float(extruder_delta) > 0:
                    extrusion_confirmed = True
            except (ValueError, TypeError):
                pass

        msg = "printing" if extrusion_confirmed else "printing (extrusion not yet confirmed)"
        evidence = {
            "state": "printing",
            "filament_used": filament_used,
            "extruder_position_delta": extruder_delta,
            "extrusion_confirmed": extrusion_confirmed,
            "info": ps.get("info") or status.get("info"),
        }
        return StartReadback(outcome="printing", evidence=evidence, message=msg)

    # 3. Preparing
    prep_evidence = {}
    heater_targets = {}
    for name, obj in (("extruder", ext), ("heater_bed", bed)):
        t = obj.get("target")
        if t is not None:
            try:
                if float(t) > 0:
                    heater_targets[name] = float(t)
            except (ValueError, TypeError):
                pass
    for name, key in (("extruder", "extruder_target"), ("heater_bed", "heater_bed_target")):
        if key in status:
            try:
                if float(status[key]) > 0:
                    heater_targets[name] = float(status[key])
            except (ValueError, TypeError):
                pass
    if heater_targets:
        prep_evidence["heating"] = heater_targets

    homed = toolhead.get("homed_axes") or status.get("homed_axes")
    if homed:
        prep_evidence["homing"] = homed
    elif toolhead.get("homing") or status.get("homing"):
        prep_evidence["homing"] = True

    if vsd.get("is_active") is True or vsd.get("file_path"):
        prep_evidence["virtual_sdcard"] = vsd.get("file_path") or True
    if idle.get("state") == "Printing" or status.get("idle_state") == "Printing":
        prep_evidence["idle_timeout"] = "Printing"
    if hook_state == "printing":
        prep_evidence["webhooks"] = "Printing"
    if ps.get("filename"):
        prep_evidence["filename"] = ps.get("filename")
    if status.get("preparation_evidence") is True or status.get("prep") is True:
        prep_evidence["preparation_evidence"] = True
    if status.get("ifs_loading") or status.get("ifs_load"):
        prep_evidence["ifs_loading"] = True
    ifs_obj = status.get("zmod_ifs") if isinstance(status.get("zmod_ifs"), dict) else {}
    if ifs_obj.get("state") or ifs_obj.get("feed") or ifs_obj.get("action"):
        prep_evidence["zmod_ifs"] = ifs_obj

    if prep_evidence:
        return StartReadback(
            outcome="preparing",
            evidence=prep_evidence,
            message="printer accepted the job and is preparing (heating/loading); not a failure; do not resubmit",
        )

    # 4. Unconfirmed
    return StartReadback(
        outcome="unconfirmed",
        evidence=None,
        message="submitted, awaiting confirmation",
    )


def _preparation_evidence(st):
    """True when a heater target is set (native start preparation), False when all are 0,
    None when unreadable. Read-only; used only after exactly one submission."""
    try:
        q = _mr(st, "/printer/objects/query?extruder=target&heater_bed=target")["result"]["status"]
        targets = [float((q.get(k) or {}).get("target")) for k in ("extruder", "heater_bed")]
    except Exception:
        return None
    return any(v > 0 for v in targets)


def _wait_printing(st, remote, wait_s, prep_check=None):
    """Wait until print_stats is printing, or the start sequence settles without printing.

    prep_check (native mapped starts): heater targets set by the printer's own preparation already
    prove acceptance, so stop waiting as soon as they appear instead of sitting out the 20 s settle."""
    t0=time.monotonic(); state=None
    while time.monotonic()-t0 < wait_s:
        state,fn,busy=_printer_phase(st)
        if state=="printing" and fn==remote: return "printing"
        if state in ("error","cancelled"): return state
        if prep_check and time.monotonic()-t0 > 2:
            try:
                if prep_check(st) is True: return state
            except Exception: pass
        if time.monotonic()-t0 > 20 and busy != "Printing": return state  # settled, not printing
        time.sleep(1)
    return "busy" if state == "printing" else state  # printing some other file is not this start


def live_snapshot(st):
    """Current temps and every IFS slot for Job Details. Read-only; unknown stays unknown."""
    snap={"nozzle_c":None,"bed_c":None,"slots":"unknown"}
    try:
        q=_mr(st,"/printer/objects/query?extruder&heater_bed")["result"]["status"]
        snap["nozzle_c"]=q.get("extruder",{}).get("temperature")
        snap["bed_c"]=q.get("heater_bed",{}).get("temperature")
    except Exception: pass
    if not _native(): return snap
    try:
        tel=read_filament_telemetry(st); palette=tel.get("configured_palette")
        presence={}
        try: presence=read_stock_filament_slots(st)
        except Exception: pass
        if isinstance(palette,list):
            snap["slots"]=[{"slot":p.get("ID"),"color":p.get("Color"),"material":p.get("Material"),
                            "loaded":presence.get(int(p["ID"])) if str(p.get("ID")).isdigit() else None}
                           for p in palette]
    except Exception: pass
    return snap


def _fmt_slots(slots):
    if not isinstance(slots,list) or not slots: return "unknown"
    def u(v): return v if v not in (None,"") else "unknown"
    def loaded(v): return "loaded" if v is True else "empty" if v is False else "presence unknown"
    return "; ".join(f"{u(s.get('slot'))}: {u(s.get('color'))} {u(s.get('material'))} ({loaded(s.get('loaded'))})" for s in slots)


# Typical durations only, from common Klipper guidance; never a measurement of this printer.
ROUTINE_TYPICAL_MIN = {
    "home": (1, 2), "bed_mesh": (3, 6), "leveling": (5, 10), "z_offset": (3, 5),
    "pid_hotend": (5, 8), "pid_bed": (8, 15), "input_shaper": (5, 10),
}
ROUTINE_NAMES = {"home": "homing", "bed_mesh": "bed mesh probe", "leveling": "full bed leveling",
                 "z_offset": "Z offset calibration", "pid_hotend": "hotend PID tuning",
                 "pid_bed": "bed PID tuning", "input_shaper": "input shaper calibration"}


def routine_receipt(routine):
    """Deterministic immediate receipt; the estimate comes only from the static typical table."""
    if routine not in ROUTINE_TYPICAL_MIN: raise ValueError(f"unknown routine {routine!r}; known: {', '.join(ROUTINE_TYPICAL_MIN)}")
    lo,hi=ROUTINE_TYPICAL_MIN[routine]
    return f"Running {ROUTINE_NAMES[routine]}, about {lo} to {hi} min (typical, not measured on this printer)."


def routine_phase(st, accepted=True):
    """accepted -> running -> completed, read from Klipper idle_timeout. Never inferred from time."""
    try: busy=_mr(st,"/printer/objects/query?idle_timeout")["result"]["status"]["idle_timeout"].get("state")
    except Exception: return "unknown"
    if busy=="Printing": return "running"
    if busy in ("Ready","Idle"): return "completed" if accepted else "idle"
    return "unknown"


def _consume_approval(job_path, job, gcode, reprint=False):
    """Single-use bed-clear approval per G-code. Refuses reuse unless an explicit new reprint approval.
    Check and record run in one launch-state transaction on the current JOB.json, never the caller's
    possibly stale snapshot; the database copy survives another writer saving an older JOB.json."""
    sha=file_sha256(gcode); key=str(Path(job_path).resolve())
    with launch_state.transaction(_job_printer_id(job)) as con:
        current=json.loads(Path(job_path).read_text())
        require_current_artifact(current, gcode)
        now=time.strftime("%Y-%m-%dT%H:%M:%S%z")
        used=[a for a in current.get("approvals",[]) if a.get("gcode_sha256")==sha and a.get("consumed_at")]
        used+=launch_state.consumed(con, key, sha)
        if used and not reprint:
            raise SystemExit(f"REFUSED: the bed-clear approval for this G-code was already used at {used[-1]['consumed_at']}. "
                             "Ask the operator for a new bed-clear confirmation, then rerun with --reprint. Nothing uploaded or started.")
        kind="reprint" if used else "bed_clear"
        launch_state.record_consumed(con, key, sha, kind, now)
        current.setdefault("approvals",[]).append({"gcode_sha256":sha,"granted_at":now,"consumed_at":now,"kind":kind})
        write_job(Path(job_path).parent, current)
    job.clear(); job.update(current)


def _printer_key(st=None, job=None):
    """The submission-lease identity for a launch: the job's roster printer id (#334).

    Kept under its old name for the callers and tests that keyed a lease by
    the hardware record; the record no longer names the lease — the job's
    printer does, and a job without one belongs to the default printer.
    """
    return _job_printer_id(job)


def _require_idle_before_mapped_launch(require_idle_settled, read_moonraker, read_native):
    """Report only an explicit idle-guard refusal as a clean pre-upload refusal."""
    try:
        require_idle_settled(read_moonraker, read_native)
    except RuntimeError as exc:
        message = str(exc)
        if not message.startswith("REFUSED: idle guard failed:"):
            raise
        raise SystemExit(message + ". Nothing uploaded or started.") from None


def _native_file_matches(reported, remote):
    """Match only documented Klipper paths for this upload, including ZMod's 3MF unpack path."""
    return launch_state.same_remote(reported, remote)


def _moonraker_started_files(st):
    """Moonraker's recent job history as (filename, start_epoch) pairs, for settling an uncertain
    earlier start (launch_state.reached_printer). Unreadable history settles nothing."""
    try:
        jobs = _mr(st, '/server/history/list?limit=20&order=desc', timeout=10)['result']['jobs']
        return [(j.get('filename'), j.get('start_time')) for j in jobs
                if isinstance(j, dict) and j.get('filename') and isinstance(j.get('start_time'), (int, float))]
    except Exception:
        return []


def _read_start_readback(st, remote, *, native=True):
    """(readable, readback): one status read after a start request that did not answer cleanly.

    The readback is classified only when print_stats names this exact upload (never another job's);
    it is None when the printer shows a different file, and `readable` is False when it could not be read.
    """
    try:
        query = "/printer/objects/query?print_stats&extruder&heater_bed&virtual_sdcard&idle_timeout&webhooks&toolhead"
        snap = _mr(st, query + ("&zmod_ifs" if native else ""))  # generic Klipper has no ZMod objects
        ps = (snap.get("result", {}).get("status", {}) or {}).get("print_stats") or {}
        if not _native_file_matches(ps.get("filename"), remote):
            return True, None
        return True, classify_start_readback(snap)
    except Exception:
        return False, None


def _reconcile_native_submission(st, remote):
    """Read status once after lost POST response; never attribute heaters alone to this job."""
    return _read_start_readback(st, remote)[1]


def _reconcile_lost_start(st, remote, *, heaters_were_off=True, native=True):
    """What the printer says about one start request whose response was lost or rejected.

    Shared by the mapped native path (#688) and the generic Moonraker path. Evidence, in order: the printer
    reports this exact file (launch_state.same_remote via the readback), then heater targets set from a
    baseline that proved every heater was off before the request, else nothing: pending. Heater targets
    alone never count when the baseline was not proven, since they may belong to unrelated work.

    Returns the outcome fields (phase, outcome, accepted, started, message, optional readback) plus `quiet`:
    True only when the printer was readable, does not report this file and has no heater target, which is
    what lets an HTTP 4xx answer be treated as "definitely not started".
    """
    readable, readback = _read_start_readback(st, remote, native=native)
    heating = _preparation_evidence(st) if readback is None else None
    quiet = readable and readback is None and heating is False
    if readback is None and heaters_were_off and heating is True:
        # The idle guard proved every heater target was 0 before this one submission, so targets set now
        # are this job's preparation (2026-10-30: the screenless SET_ZCOLOR POST held open through warmup
        # and timed out after 30 s).
        return {'phase': 'preparing', 'outcome': 'preparing', 'accepted': True, 'started': True,
                'preparation_evidence': True, 'quiet': False,
                'message': 'start response was lost; the printer is heating for this job'}
    if readback is None:
        return {'phase': 'preparing_or_unresolved', 'outcome': 'unknown', 'accepted': False, 'started': False,
                'quiet': quiet,
                'message': 'start response was lost; printer status does not yet identify this file; do not resubmit'}
    fields = {'readback': readback._asdict(), 'quiet': False}
    if readback.outcome == 'printing':
        return {'phase': 'executing', 'outcome': 'printing', 'accepted': True, 'started': True,
                'message': readback.message, **fields}
    if readback.outcome == 'preparing':
        return {'phase': 'preparing', 'outcome': 'preparing', 'accepted': True, 'started': True,
                'message': readback.message, **fields}
    return {'phase': 'preparing_or_unresolved', 'outcome': 'unknown', 'accepted': False, 'started': False,
            'message': 'the submitted file is visible but start progress is unresolved; do not resubmit', **fields}


def _start_rejection(exc):
    """The printer's own words when `exc` is a definitive refusal of the start request, else None.

    Only an HTTP 4xx answer qualifies: the request arrived, was read and was refused (Moonraker 400
    "Must home", 404 file not found). A timeout, a reset connection and every 5xx may have reached a
    printer that went on to start, so they stay uncertain. 408, 425 and 429 describe the transport,
    not the printer's decision, and stay uncertain too.
    """
    if not isinstance(exc, urllib.error.HTTPError) or not 400 <= exc.code < 500 or exc.code in (408, 425, 429):
        return None
    detail = ""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        error = body.get("error") if isinstance(body, dict) else None
        detail = error.get("message") if isinstance(error, dict) else (error if isinstance(error, str) else "")
    except Exception:
        pass
    return f"HTTP {exc.code}: {' '.join(str(detail or exc.reason or 'rejected').split())}"[:300]


def _settle_lost_start(lease, st, remote, exc, *, base, heaters_were_off, first_command=True, native=True):
    """Settle a start request that raised `exc`: returns (out, rejection).

    Anything but a clean 4xx on the first command leaves the lease uncertain (never released by guessing)
    and reconciles from the printer, like the mapped native path always did. A 4xx answer on a printer
    that shows nothing happening releases the lease and the start claim and returns (None, message):
    the caller ends as REFUSED, with the printer's words. A 4xx on a printer that shows activity is
    uncertain after all.
    """
    rejection = _start_rejection(exc) if first_command else None
    if rejection is None:
        lease.uncertain(f'start request response lost: {exc!r}')
    # Moonraker answers 400 when a macro errors partway, heaters left on: after a rejection only a
    # readback naming this exact file may say started; heater targets alone are never evidence.
    verdict = _reconcile_lost_start(st, remote, heaters_were_off=heaters_were_off and rejection is None,
                                    native=native)
    quiet = verdict.pop('quiet')
    if rejection is not None:
        if quiet:
            lease.release()  # the printer refused it and shows no sign of work: nothing started
            release_start_announcement(remote)
            return None, rejection
        lease.uncertain(f'start request rejected ({rejection}) but the printer shows activity: {exc!r}')
    out = {**base, **verdict}
    if not out['accepted']:
        lease.uncertain(out['message'])
        # Nobody has announced this start, so let the listener post the "Print started" card
        # when print_stats reaches this file.
        release_start_announcement(remote)
    return out, rejection


def _rejected_start_message(rejection):
    return (f"REFUSED: the printer rejected the start request ({rejection}). "
            "The file was uploaded but nothing started.")


START_OUTCOME_MARK = "START READBACK "  # "MAPPED START READBACK " contains it, so one parser reads both
_OUTCOME_KEYS = ("phase", "outcome", "started", "accepted", "file", "method", "message")


def _emit_start_outcome(out, mark=START_OUTCOME_MARK, compact=True):
    """The one uniform outcome line every launch ends with: `START READBACK {json with phase}`.

    prepared_intake and print_next read the phase from it to tell a confirmed start from an unresolved one.
    """
    line = {k: out[k] for k in _OUTCOME_KEYS if k in out} if compact else dict(out)
    if isinstance(line.get('message'), str):
        line['message'] = line['message'][:200]
    print(mark + json.dumps(line, default=str), flush=True)


def _emit_mapped_outcome(out):
    """The mapped route's outcome: the bulky record on an earlier `MAPPED START DETAIL` line (the checkpoint
    file holds it too), then the compact `MAPPED START READBACK` line last. print_next keeps only the last
    1500 characters of output, so the line the intake parses must stay short."""
    print('MAPPED START DETAIL ' + json.dumps(out, default=str), flush=True)
    _emit_start_outcome(out, mark='MAPPED START READBACK ')


def _uncertain_after_submission(lease, remote, exc, *, method, phase='awaiting_confirmation', extra=None,
                                mark=START_OUTCOME_MARK):
    """A failure after the start request went out (a readback that raised, a Klippy restart, a 503).

    The printer may be starting, so nothing is retried: the lease becomes uncertain, the start-announcement
    claim is released (nobody announced this start; the listener posts it when the printer reaches the
    file, which is what the chat was told), and the run ends with a clean UNCERTAIN line and outcome.
    """
    reason = ' '.join(str(exc).split()) or type(exc).__name__
    try:
        lease.uncertain(f'readback after the start request failed: {reason}')
    except Exception as lease_exc:
        logger.warning('could not mark the lease uncertain: %s', lease_exc)
    release_start_announcement(remote)
    message = f'the start was sent, but reading the printer back failed ({reason}); do not resubmit'
    out = {**(extra or {}), 'started': False, 'accepted': False, 'file': remote, 'method': method,
           'phase': phase, 'outcome': 'unknown', 'message': message}
    print('UNCERTAIN: ' + message, flush=True)
    if extra is not None:
        print(mark.replace('READBACK', 'DETAIL') + json.dumps(out, default=str), flush=True)
    _emit_start_outcome(out, mark=mark)
    return out


def _submission_failed(lease, remote, label, exc):
    """Upload or start raised for a transport whose client collapses both into one step: uncertain,
    claim released, outcome line printed, never resubmitted. Raises."""
    lease.uncertain(f"{label} submission did not complete: {exc!r}")
    release_start_announcement(remote)
    _emit_start_outcome({'started': False, 'accepted': False, 'file': remote, 'phase': 'awaiting_confirmation',
                         'outcome': 'unknown', 'message': f'{label} submission did not complete; do not resubmit'})
    if isinstance(exc, RuntimeError):
        raise SystemExit(f"{label} submission failed or uncertain: {exc}. Do not resubmit without "
                         "checking printer status.") from exc
    raise exc


# Start confirmation by polling (#271 for Bambu, now every transport): one immediate readback after the
# start request is not evidence either way, since a printer takes seconds to minutes to report the file.
START_CONFIRM_DEFAULT_S = 45
START_CONFIRM_POLL_S = 3
# Normalized states that mean "accepted, still starting" (heating the bed, running its calibration).
START_TRANSITIONAL_STATES = frozenset({"heating", "calibrate_doing", "busy", "preparing", "starting"})


def _start_confirm_window_s():
    """How long a non-Bambu start is polled (Bambu has [bambu] start_confirm_s)."""
    return float(START_CONFIRM_DEFAULT_S)


class StartPoll(NamedTuple):
    confirmed: bool    # the printer reports this file printing or preparing
    preparing: bool    # ... and is still in a transitional state (heating, calibrating)
    live: dict         # the last readable normalized status
    snapshots: int
    window_s: float


def _reports_started(live, remote):
    """Printing or a transitional state, naming this upload (launch_state.same_remote)."""
    state = live.get("state")
    return ((state == "printing" or state in START_TRANSITIONAL_STATES)
            and launch_state.same_remote(live.get("filename"), remote))


def _poll_start_confirmation(read_status, remote, *, window_s=None, poll_s=START_CONFIRM_POLL_S,
                             reports=_reports_started, job_path=None, stage="start_confirm",
                             evidence_of=None, on_snapshot=None):
    """Poll `read_status()` (a normalized status dict) until `reports(live, remote)` or `window_s` ends.

    Never raises for a bad read: an exception counts as a snapshot, the window keeps running and the last
    readable status is what is reported. Every snapshot is written to the job's TIMING.jsonl so an
    unconfirmed start leaves evidence of what the printer said. Both clock hooks are module attributes so
    tests drive the window without sleeping.
    """
    window_s = _start_confirm_window_s() if window_s is None else float(window_s)
    started = _confirm_clock()
    deadline = started + window_s
    last_good, snapshots = {}, 0
    while True:
        snapshots += 1
        error = None
        try:
            live = read_status()
            if not isinstance(live, dict):
                raise RuntimeError("the printer returned no readable status")
            last_good = live
        except Exception as exc:
            live, error = {}, " ".join(str(exc).split()) or type(exc).__name__
        evidence = {"snapshot": snapshots, "elapsed_s": round(_confirm_clock() - started, 1),
                    **(evidence_of(live) if evidence_of else {"state": live.get("state"), "file": live.get("filename")})}
        if error:
            evidence["error"] = error
        if on_snapshot:
            on_snapshot(evidence)
        _timing(job_path, stage, **evidence)
        if reports(live, remote):
            return StartPoll(True, live.get("state") in START_TRANSITIONAL_STATES, live, snapshots, window_s)
        remaining = deadline - _confirm_clock()
        if remaining <= 0:
            return StartPoll(False, False, last_good, snapshots, window_s)
        _confirm_sleep(min(poll_s, remaining))


def _settle_polled_start(lease, poll, remote, *, method, uncertain_detail=None, **extra):
    """The lease and the outcome dict for a polled start.

    Confirmed (printing, or accepted and still heating/calibrating) releases the lease: the busy printer
    refuses another start. Anything else leaves it uncertain and frees the start claim so the listener
    posts the start when the printer reaches the file. Nothing here ever resubmits.
    """
    if poll.confirmed:
        lease.release()
        if poll.preparing:
            phase, outcome = "preparing", "preparing"
            message = "printer accepted the job and is preparing (heating/calibrating); not a failure; do not resubmit"
        else:
            phase, outcome, message = "executing", "printing", "printing"
    else:
        lease.uncertain(uncertain_detail or (
            f"submitted, awaiting confirmation; printer reported state {poll.live.get('state') or 'unknown'}, "
            f"file {poll.live.get('filename') or 'none'!r} on the last of {poll.snapshots} snapshot(s) "
            f"over {poll.window_s:g}s"))
        release_start_announcement(remote)
        phase, outcome = "awaiting_confirmation", "unconfirmed"
        message = "submitted, awaiting confirmation; do not resubmit"
    return {"started": poll.confirmed, "accepted": True, "file": remote, "method": method, "phase": phase,
            "outcome": outcome, "live": poll.live, "message": message, **extra}


def _announce_polled_outcome(out):
    """The legacy human line plus the uniform outcome line, last so a 6-line tail always holds it."""
    print(("PRINT STARTED " if out["phase"] == "executing" else "START SUBMITTED; AWAITING CONFIRMATION ")
          + json.dumps(out, default=str))
    _emit_start_outcome(out)


CHECKPOINT_NOT_SENT = ("rejected", "not_submitted")  # launch attempts that provably never reached the printer


def _require_launch_checkpoint_clear(checkpoint, reprint):
    """`<plate>.launch.json` records one launch attempt (print-again reads it too), so it is never deleted.

    An earlier attempt's file must not stop a deliberate reprint (multi-copy plates, "Print again"): the
    operator's new bed-clear approval is exactly that decision, and the printer lease already refused if
    the earlier attempt is still unsettled. It does stop an unapproved replay of the same artifact, and
    an attempt that never reached the printer never blocks anything.
    """
    if not checkpoint.exists():
        return
    try:
        phase = json.loads(checkpoint.read_text()).get('phase')
    except (OSError, ValueError):
        phase = None
    if phase in CHECKPOINT_NOT_SENT or reprint:
        return
    raise RuntimeError('mapped submission checkpoint exists; reconcile or pass an explicit reprint approval '
                       'instead of replaying')


def _write_checkpoint(checkpoint, data):
    try:
        checkpoint.write_text(json.dumps(data, indent=2) + '\n')
    except OSError as exc:
        logger.warning('could not write the launch checkpoint %s: %s', checkpoint, exc)


def _check_mapped_slot_color(slot, tool, live_raw, expected):
    """The printer screen's slot color against the recorded one, before a mapped launch.

    The two are compared as colors, not strings (FFFFFF == FAFAFA, 0C6283FF == 0C6283),
    and only a clear difference between two real hexes refuses. A missing, unreadable
    or merely close value prints a WARNING and proceeds, and so does a difference from
    a record hex we guessed ourselves (``hex_source`` name/default): that is our own
    guess disagreeing with the screen, not the operator's evidence of a wrong spool.
    """
    live = filament_color.normalize_hex(live_raw)
    recorded = filament_color.first_hex(expected.get('hex'))
    shown_live = f"#{live}" if live else (str(live_raw).strip() if isinstance(live_raw, str) and live_raw.strip() else 'blank')
    shown_recorded = f"#{recorded}" if recorded else 'no color code'
    verdict = filament_color.compare_colors(live, recorded)
    if verdict == 'same':
        return
    if verdict == 'different' and expected.get('hex_source') not in filament_color.DERIVED_SOURCES:
        raise SystemExit(f"REFUSED: printer slot {slot} color is {shown_live} but Spoolman has "
                         f"{shown_recorded} there (T{tool}). Nothing uploaded or started.")
    reason = {'unknown': 'the colors could not be compared',
              'close': 'the colors are close but not identical',
              'different': 'the recorded color code was worked out from the spool name, not read from Spoolman'}[verdict]
    print(f"WARNING: printer slot {slot} shows {shown_live} but the record has {shown_recorded} "
          f"(T{tool}); {reason}, so the launch continues.", flush=True)


def print_mapped_native(g, st, mesh, mapping, job, job_path, wait_s, reprint):
    """Native explicit mapping route; exactly one SET_ZCOLOR submission, no fallback."""
    adapter = _job_adapter(job)
    require_current_artifact(job, g)
    display_off = mesh.get('display_off')
    if display_off not in (0, 1):
        raise SystemExit(f"REFUSED: ZMod reports an unknown screen mode (display_off={display_off!r}), "
                         "so Print Handy cannot tell where slot and idle state come from. "
                         "Nothing uploaded or started.")
    # Screenless (display_off=1: HelixScreen or no screen) the stock firmware is not
    # running and its /detail API refuses, so idle and slot presence come from Klipper
    # alone. ZMod's own print-file macro starts AD5X prints with SET_ZCOLOR in both
    # modes; [zmod_color] runs with display: False when screenless.
    screenless = display_off == 1
    text = g.read_text()
    _embedded = adapter.native.embedded_mapping
    _embedded(text, st)  # refuses a spool swapped since slicing, before any printer I/O
    validate_saved_mesh_start(text, mesh)
    response = _mr(st, '/printer/objects/query?webhooks&print_stats&extruder&heater_bed&zmod_color&filament_switch_sensor%20head_switch_sensor')
    status = response['result']['status']
    def read_moonraker():
        current = _mr(st, '/printer/objects/query?webhooks&print_stats&extruder&heater_bed&zmod_color&filament_switch_sensor%20head_switch_sensor')['result']['status']
        return {'state': current['webhooks']['state'], 'print': current['print_stats'],
                'extruder': current['extruder'], 'bed': current['heater_bed']}
    if screenless:
        require_idle_screenless = adapter.native.require_idle_screenless
        def require_idle():
            _require_idle_before_mapped_launch(lambda *_: require_idle_screenless(read_moonraker),
                                               read_moonraker, None)
    else:
        require_idle_settled = adapter.native.require_idle_settled
        def read_native():
            return read_stock_detail(st)
        def require_idle():
            _require_idle_before_mapped_launch(require_idle_settled, read_moonraker, read_native)
    require_idle()
    status = response['result']['status']
    sensor = status.get('filament_switch_sensor head_switch_sensor', {})
    if sensor.get('enabled') is not True:
        raise RuntimeError('head filament sensor protection is disabled or unknown')
    # Idle nozzle engagement is not feeder occupancy. Genuine native errors and
    # installed runout/jam protections stay intact; do not actuate sensor overrides.
    configured = status.get('zmod_color', {}).get('slots', [])
    if screenless:
        occupancy = adapter.native.read_zmod_color_slots(status)
    else:
        occupancy = read_stock_filament_slots_from_detail(read_native())
    normalize_mapping = adapter.native.normalize_mapping
    label_matches = adapter.native.screen_label_matches
    mapped = normalize_mapping(mapping, st)
    for tool, slot in mapped.items():
        expected = st['filament_slots'][str(slot)]
        matches = [item for item in configured if str(item.get('ID')) == str(slot)]
        # The native slot label is coarser than Spoolman's material: family match
        # (PLA == PLA+), or the screen's own type for it (SILK for Silk PLA).
        if len(matches) != 1:
            raise SystemExit(f"REFUSED: printer slot {slot} is missing from the printer's slot list (T{tool}). "
                             "Nothing uploaded or started.")
        label = matches[0].get('Material')
        if not label_matches(label, expected['material']):
            raise SystemExit(f"REFUSED: printer slot {slot} is labeled {label or 'blank'} but Spoolman has "
                             f"{expected['material']} there (T{tool}). Nothing uploaded or started.")
        _check_mapped_slot_color(slot, tool, matches[0].get('HEX'), expected)
        if occupancy.get(slot) is not True:
            # Operator-confirmed loaded feeders supersede idle false occupancy,
            # never unknown telemetry or an actual native error/activity.
            if occupancy.get(slot) is not False or expected.get('physical_loading_verified') is not True:
                raise RuntimeError(f'T{tool} slot {slot} occupancy is unknown or unconfirmed')
    sha = file_sha256(g)
    remote = remote_name(g, sha, job)
    upload_path, upload_sha, remote, delivery = _zmod_upload_artifact(adapter, st, g, sha, remote)
    lease = launch_state.acquire(_printer_key(st, job), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=(status.get('print_stats') or {}).get('filename'),
                                 started_files=lambda: _moonraker_started_files(st))
    checkpoint, checkpoint_data = g.with_suffix('.launch.json'), None
    try:
        # Again inside the lease: the first read may predate another job's start.
        require_idle()
        if job_path is not None:
            _consume_approval(job_path, job, g, reprint=reprint)
        _require_launch_checkpoint_clear(checkpoint, reprint)
        slots_used = sorted(set(mapped.values()))
        if len(slots_used) > 1:
            print('WARNING: MULTI-SLOT JOB: automatic Spoolman usage deduction disabled; aggregate usage cannot be safely apportioned to selected spools.', flush=True)
        else:
            sync = _sync_active_spool(st, slots_used[0])
            if sync.get('spoolman_pending') and job_path is not None:
                # Usage could not be pointed at this job's spool (#387):
                # record it as pending in the job record for later
                # reconciliation. Recording must never block the launch.
                job['spoolman_usage_pending'] = {
                    'spool_id': sync.get('spool_id'), 'slot': slots_used[0],
                    'reason': 'Spoolman unavailable at print start; usage not yet reported',
                    'recorded_at': time.time()}
                try:
                    write_job(Path(job_path).parent, job)
                except Exception as exc:
                    logger.warning('could not record pending Spoolman usage: %s', exc)
        _timing(job_path, 'upload_begin')
        uploaded = subprocess.run([str(MOONRAKERCTL), 'upload', str(upload_path), remote, upload_sha],
                                  text=True, capture_output=True, timeout=300, env=_moonrakerctl_env(st))
        if uploaded.returncode:
            raise RuntimeError('The file could not be uploaded to the printer, so nothing started. '
                               'Check the printer is on and reachable, then try again.'
                               + _detail_tail(uploaded.stderr or uploaded.stdout))
        upload_json = json.loads(uploaded.stdout)
        g.with_suffix('.upload.json').write_text(json.dumps(upload_json, indent=2) + '\n')
        result = upload_json.get('result', upload_json)
        item = result.get('item', {}) if isinstance(result, dict) else {}
        if item.get('root') != 'gcodes' or item.get('path') != remote:
            raise RuntimeError('uploaded target mismatch: ' + json.dumps(item))
        # Integrity: Moonraker checked the SHA256 we sent and refuses the upload (HTTP 400) on mismatch,
        # so the file is never downloaded back to hash it.
        _timing(job_path, 'upload_verified')
        if delivery: _note_delivery_format(job, job_path, delivery)
        command = (f'SET_ZCOLOR FILENAME={remote} SILENT=1 AUTO_ASSIGN=0 LEVELING=0 ' +
                   ' '.join(f'T{tool}={slot}' for tool, slot in sorted(mapping.items())))
        # One checkpoint per launch attempt: keyed by this lease's token, so a deliberate reprint
        # replaces the earlier attempt's record instead of colliding with it.
        checkpoint_data = {'phase': 'submission_pending', 'command': command, 'sha256': sha,
                           'file': remote, 'artifact': str(g), 'submission_count': 1,
                           'accepted': False, 'started': False, 'lease_token': lease.token}
        checkpoint.write_text(json.dumps(checkpoint_data, indent=2) + '\n')
        # This path posts its own start card, so the listener must not post a second one
        # when native preparation (heating, leveling) finishes minutes later (2026-10-11 duplicate).
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        _announce_print_name(st, job, slots_used, g)
        _publish_job_card(st, job, remote, g.read_text(errors="ignore"))
    except BaseException:
        lease.release()  # nothing was sent to start
        release_start_announcement(remote)  # the claim was taken in the block above
        if checkpoint_data is not None:
            _write_checkpoint(checkpoint, {**checkpoint_data, 'phase': 'not_submitted'})
        raise
    lease.submitting()
    try:
        receipt = _mr(st, '/printer/gcode/script', data={'script': command})
    except Exception as exc:
        _timing(job_path, 'start_response_lost', detail=type(exc).__name__)
        # A synchronous POST can time out after Moonraker accepted SET_ZCOLOR. Reconcile by this exact
        # submitted file only (_settle_lost_start); heater targets alone may belong to unrelated work and
        # are never attribution evidence. The idle guard proved the heaters were off before this request.
        out, rejection = _settle_lost_start(lease, st, remote, exc, base=checkpoint_data, heaters_were_off=True)
        if out is None:
            _write_checkpoint(checkpoint, {**checkpoint_data, 'phase': 'rejected', 'rejection': rejection})
            raise SystemExit(_rejected_start_message(rejection)) from exc
        if out['accepted']:
            # The POST response was lost: preserve the duplicate guard until
            # explicit reconciliation, even if a matching readback is active.
            _write_checkpoint(checkpoint, out)
            try:
                out['record_path'] = str(record_start(g, remote))
            except Exception as record_exc:
                logger.warning('start record failed after reconciled submission: %s', record_exc)
        _write_checkpoint(checkpoint, out)
        _emit_mapped_outcome(out)
        return out
    _timing(job_path, 'start_submitted')
    out = {'accepted': False, 'started': False, 'phase': 'submitted_unconfirmed',
           'file': remote, 'artifact': str(g), 'sha256': sha, 'mapping': mapping,
           'command': command, 'receipt': receipt, 'submission_count': 1, 'print_number': None,
           'lease_token': lease.token}
    try:
        return _confirm_mapped_start(st, g, lease, remote, wait_s, out, checkpoint, job_path, delivery)
    except Exception as exc:
        # The request is out: a transient 503 or a Klippy restart while reading back is not a traceback
        # and not a refusal. Uncertain, never resubmitted, claim freed for the listener.
        out = _uncertain_after_submission(lease, remote, exc, method='mapped_native',
                                          phase='preparing_or_unresolved', extra=dict(out),
                                          mark='MAPPED START READBACK ')
        _write_checkpoint(checkpoint, out)
        return out


def _confirm_mapped_start(st, g, lease, remote, wait_s, out, checkpoint, job_path, delivery):
    """Everything after the one SET_ZCOLOR request was accepted: number, record, wait, classify, settle."""
    # Number at submission, not at "printing": native preparation can outlast the readback
    # and the listener's completion card only reads the registry (2026-10-14: 6 unnumbered).
    try:
        out['print_number'] = _assign_print_number(job_path)
    except Exception as exc:
        logger.warning('could not assign a print number: %s', exc)
    if delivery:
        out['delivery_format'] = delivery
    _write_checkpoint(checkpoint, out)
    print('MAPPED START SUBMITTED ' + json.dumps(out), flush=True)
    # Record immediately so a long native preparation cannot lose its start record.
    try:
        out['record_path'] = str(record_start(g, remote))
    except Exception as exc:
        logger.warning('start record failed after submission: %s', exc)
    state = _wait_printing(st, remote, wait_s, prep_check=_preparation_evidence)
    readback = None
    try:
        snap = _mr(st, "/printer/objects/query?print_stats&extruder&heater_bed&virtual_sdcard&idle_timeout&webhooks&toolhead")
        ps = snap.get('result', {}).get('status', {}).get('print_stats', {})
        if _native_file_matches(ps.get('filename'), remote):
            readback = classify_start_readback(snap)
    except Exception:
        pass

    if state == 'printing' or (readback and readback.outcome == 'printing'):
        out.update(accepted=True, started=True, phase='executing', moonraker_state='printing',
                   outcome='printing', message=(readback.message if readback else 'printing'),
                   extrusion_confirmed=bool(readback and (readback.evidence or {}).get('extrusion_confirmed')))
    else:
        # The AD5X native route heats and loads the IFS slot for several minutes before
        # print_stats reports printing (2026-10-16: ~7 min). The idle guard proved heaters
        # were off before this one submission, so targets set now prove acceptance.
        preparing = _preparation_evidence(st)
        is_prep = preparing is True or bool(readback and readback.outcome == 'preparing')
        outcome_val = 'preparing' if is_prep else (readback.outcome if readback else 'unconfirmed')
        msg = readback.message if readback else 'submitted, awaiting confirmation'
        out.update(accepted=is_prep, started=is_prep, moonraker_state=state,
                   phase='preparing' if is_prep else 'preparing_or_unresolved',
                   outcome=outcome_val,
                   message=msg, preparation_evidence=preparing)
    _write_checkpoint(checkpoint, out)
    if out['accepted']:
        lease.release()  # accepted; the busy printer now refuses another start
    else:
        lease.uncertain(out['message'])  # reconciled when the printer reports this file
        release_start_announcement(remote)  # unannounced: the listener posts the start card
    _emit_mapped_outcome(out)
    return out


def resolve_print_target(target, archive=None):
    """A bare printer file name (what Moonraker shows) maps to the archived local artifact
    that was uploaded under that name. Never searches outside the archive, never downloads."""
    raw = str(target)
    p = Path(raw).expanduser()
    if p.is_absolute() or "/" in raw or p.exists():
        return raw
    archive = Path(archive or ARCHIVE)
    for pat in ("*/sliced/*/*/*.launch.json", "*/sliced/*/*/*.upload.json"):
        for rec in sorted(archive.glob(pat), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                d = json.loads(rec.read_text())
            except (OSError, ValueError):
                continue
            item = d.get("item") if isinstance(d.get("item"), dict) else {}
            if raw in (d.get("file"), d.get("remote"), d.get("remote_name"), item.get("path")):
                artifact = rec.with_name(rec.name.split(".", 1)[0] + ".gcode")
                if artifact.is_file():
                    return str(artifact)
    raise SystemExit(f"REFUSED: {raw} is a printer file name, not a local path, and no archived job uploaded a "
                     "file with that name. Pass the job's JOB.json instead. Nothing uploaded or started.")


def remap_slot(target, new_slot, tool="0", state=None):
    """Remap a single-color job or G-code to a different physical IFS slot.

    Rewrites tool mapping metadata and checksum, updating job filament_selection
    when given a JOB.json, allowing printing with a different physical spool
    without physical changes or screen interaction.
    """
    st = state or printer_state.load()
    resolved = resolve_print_target(target)
    target_path = Path(resolved).expanduser().resolve()

    job = None
    job_path = None
    if target_path.name == "JOB.json":
        job_path = target_path
        job = json.loads(job_path.read_text())
        if not job.get("gcode"):
            raise RuntimeError("stale color artifact: job has no sliced gcode")
        gcode_path = Path(job["gcode"]).expanduser().resolve()
    else:
        gcode_path = target_path
        temp_root = Path(os.environ.get("TMPDIR") or "/tmp").resolve()
        for parent in list(gcode_path.parents)[:5]:
            if parent in (Path("/"), temp_root) or temp_root in parent.parents:
                break
            if (parent / "JOB.json").is_file():
                job_path = parent / "JOB.json"
                job = json.loads(job_path.read_text())
                break

    slot_int = int(new_slot)
    if slot_int not in range(1, FILAMENT_SLOTS + 1):
        raise ValueError(f"physical IFS slot must be an integer from 1 through {FILAMENT_SLOTS}")

    selection = resolve_filament_selection(st, {"slot": slot_int})

    raw_tools = {}
    if gcode_path.is_file():
        for line in gcode_path.read_text().splitlines():
            if line.startswith(MAP_PREFIX):
                try:
                    data = json.loads(line[len(MAP_PREFIX):])
                    for t, v in data.items():
                        raw_tools[str(t)] = v.get("physical_slot") if isinstance(v, dict) else v
                except Exception:
                    pass
                break
    mapping = dict(raw_tools) if raw_tools else {}
    mapping[str(tool)] = slot_int
    normalize_mapping = printers.current().native.normalize_mapping
    mapping = normalize_mapping(mapping, st)

    new_sha = None
    if gcode_path.is_file():
        finalize = printers.current().native.finalize
        new_sha = finalize(gcode_path, None, st, mapping)
        try:
            verify_gcode(gcode_path)
        except Exception:
            offline_st = dict(st)
            offline_st.setdefault("temps_c", {"bed": 60, "nozzle": 210})
            verify_gcode_offline(gcode_path, offline_st)

    if job is not None and job_path is not None:
        job["filament_selection"] = selection
        if new_sha:
            job["gcode_sha256"] = new_sha
            job["gcode_verified"] = True
            if "artifact_binding" in job and isinstance(job["artifact_binding"], dict):
                job["artifact_binding"]["gcode_sha256"] = new_sha
                job["artifact_binding"]["decisions_sha256"] = decisions_digest(job.get("color_decisions"))
        write_job(job_path.parent, job)

    return {
        "ok": True,
        "slot": slot_int,
        "tool": str(tool),
        "mapping": mapping,
        "selection": selection,
        "gcode": str(gcode_path) if gcode_path.is_file() else None,
        "gcode_sha256": new_sha,
        "job_json": str(job_path) if job_path else None,
    }


def _print_octoprint(adapter, state, gcode, job, job_path, wait_s, reprint):
    """A separate transport path, retaining artifact and approval guards above."""
    client = adapter.client()
    try:
        status = client.status()
        client.require_ready(status)
        observed = adapter.normalize_status(status).get('filename')
        verify_gcode(gcode)
        _refuse_multicolor(gcode.read_text())
    except RuntimeError as exc:
        raise SystemExit(f'REFUSED: {exc}. Nothing uploaded or started.') from exc
    sha = file_sha256(gcode)
    remote = remote_name(gcode, sha, job)
    lease = launch_state.acquire(_printer_key(state, job), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=observed)
    try:
        try:
            client.require_ready(client.status())  # again inside the lease
        except RuntimeError as exc:
            raise SystemExit(f'REFUSED: {exc}. Nothing uploaded or started.') from exc
        if job_path is not None:
            _consume_approval(job_path, job, gcode, reprint=reprint)
    except BaseException:
        lease.release()
        raise
    # OctoPrint preheats from these, so they are the G-code spool's, resolved before anything is sent.
    temps = _with_gcode_temperatures(state, Path(gcode).read_text(errors='ignore'))['temps_c']
    # Upload and start are one uncertain step here: the client reports either failure the same way.
    lease.submitting()
    try:
        remote = client.upload(gcode, remote)
        from .nozzle import effective_nozzle_temperature
        hotend = effective_nozzle_temperature((state.get('nozzle') or {}).get('type', 'hardened_steel'), temps['nozzle'])
        # Claim the start (#460): the family listener must not post a second start for this print.
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        client.start(remote, temps['bed'], hotend)
    except BaseException as exc:
        _submission_failed(lease, remote, 'OctoPrint', exc)
    # Accepted is not proof of printing: poll the readback over the confirmation window. No resubmission.
    poll = _poll_start_confirmation(lambda: adapter.normalize_status(client.status()), remote,
                                    job_path=job_path, stage='octoprint_start_confirm')
    out = _settle_polled_start(lease, poll, remote, method='octoprint')
    confirmed = out['started']
    if confirmed:
        try:
            out['record_path'] = record_start(gcode, remote)
        except Exception as exc:
            print(f'WARNING: start record failed: {exc}')
        # The print path starts a launched-print monitor (#373): it makes
        # the completion post, attaches the timelapse OctoPrint itself
        # rendered, and posts this print's pause alerts (#460). Prints
        # started outside Print Handy belong to the octoprint listener
        # domain (family_event_listener). A monitor that cannot start
        # never fails the print.
        try:
            from print_handy.hermes import octo_watch
            octo_watch.start_monitor(adapter, remote)
        except Exception as exc:
            print(f"WARNING: timelapse monitor did not start: {exc}")
    _announce_polled_outcome(out)
    return out


def _bambu_start_confirm_window():
    """[bambu] start_confirm_s: how long a Bambu start is polled for confirmation (default 90 s)."""
    value = (settings.current().get("bambu") or {}).get("start_confirm_s", BAMBU_START_CONFIRM_S)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return float(BAMBU_START_CONFIRM_S)
    return float(value)


def _bambu_reports_file(live, remote):
    """True when a normalized Bambu status shows `remote` running or preparing.

    `printing` covers gcode_state RUNNING, PREPARE and SLICING: an A1 unpacking a package sits
    in PREPARE before RUNNING, and both prove the start reached it. The file name is the
    `subtask_name` the start command set, so exact; the bare project name (the printer's
    display spelling of the same package) is accepted too.
    """
    if live.get("state") != "printing":
        return False
    reported = str(live.get("filename") or "")
    if not reported:
        return False
    accepted = {remote}
    for suffix in (".gcode.3mf", ".3mf"):
        if remote.endswith(suffix):
            accepted.add(remote[:-len(suffix)])
    return reported in accepted


def _confirm_bambu_start(client, adapter, remote, job_path, window_s, poll_s=BAMBU_START_POLL_S):
    """Poll status snapshots until the printer reports `remote` running or preparing, or `window_s` ends.

    Returns (confirmed, last normalized status, snapshots taken). The shared poll
    (_poll_start_confirmation) does the work; Bambu adds its own evidence fields (gcode_state, print_stage)
    and matcher (_bambu_reports_file). Every snapshot is logged at INFO and written to the job's
    TIMING.jsonl so an unconfirmed start leaves evidence of what the printer said, not just that it said
    nothing (#271). A read error counts as a snapshot and the window keeps running.
    """
    def log(evidence):
        error = evidence.get("error")
        logger.info("Bambu start confirmation snapshot %d at %ss: state=%s gcode_state=%s file=%r stage=%s%s",
                    evidence["snapshot"], evidence["elapsed_s"], evidence["state"], evidence["gcode_state"],
                    evidence["file"], evidence["print_stage"], f" error={error}" if error else "")

    poll = _poll_start_confirmation(
        lambda: adapter.normalize_status(client.snapshot()), remote, window_s=window_s, poll_s=poll_s,
        reports=_bambu_reports_file, job_path=job_path, stage="bambu_start_confirm", on_snapshot=log,
        evidence_of=lambda live: {"state": live.get("state"), "gcode_state": live.get("gcode_state"),
                                  "file": live.get("filename"), "print_stage": live.get("stage")})
    return poll.confirmed, poll.live, poll.snapshots


def _print_bambu(adapter, state, gcode, job, job_path, wait_s, reprint):
    """The Bambu Lab LAN print path (#187 stage d), mirroring _print_octoprint's guard order.

    Every check that can refuse cleanly runs before the submission lease: LAN configured,
    live snapshot idle with no readiness problems (the firmware signature bit — Developer
    Mode off — is one of them), G-code verified, package built by the same delivery the
    handoff uses and confirmed to hold exactly Metadata/plate_1.gcode. Inside the lease
    the snapshot and readiness are taken again, the bed-clear approval is consumed, and
    only then does anything reach the printer: FTPS upload (size-verified), then the
    project_file command. Upload + start are one uncertain step from submitting() on,
    exactly as for OctoPrint; acceptance is confirmed from fresh snapshots polled over the
    [bambu] start_confirm_s window (#271), never assumed from the command being accepted.
    """
    from . import delivery as _delivery

    def refuse(exc):
        return SystemExit(f"REFUSED: {' '.join(str(exc).split())}. Nothing uploaded or started.")

    try:
        client = adapter.client()
    except RuntimeError as exc:
        raise refuse(exc) from exc

    def ready_snapshot():
        # readiness_problems covers busy/print-error/HMS and the firmware signature bit
        # (Developer Mode off), which blocks every write including this start.
        snapshot = client.snapshot()
        problems = adapter.readiness_problems(state, snapshot)
        norm = adapter.normalize_status(snapshot)
        if norm["state"] not in ("standby", "complete"):
            problems.append(f"printer state is {norm['state']!r}, not idle")
        if problems:
            raise RuntimeError("; ".join(dict.fromkeys(problems)))
        return snapshot, norm

    try:
        _snapshot, observed_norm = ready_snapshot()
        verify_gcode(gcode)
        packages = _delivery.bambu_handoff(job if isinstance(job, dict) else {"printer_model": adapter.key},
                                            [gcode], settings.current())
        if len(packages) != 1:
            raise RuntimeError(f"expected exactly one Bambu package for this plate, got {len(packages)}")
        package = Path(packages[0])
        with zipfile.ZipFile(package) as archive:
            plates = [n for n in archive.namelist() if n.startswith("Metadata/plate_") and n.endswith(".gcode")]
        if plates != ["Metadata/plate_1.gcode"]:
            raise RuntimeError(f"package holds plates {plates or 'none'}; LAN print start supports a "
                               "single-plate package (Metadata/plate_1.gcode) only")
    except RuntimeError as exc:
        raise refuse(exc) from exc
    remote = package.name
    sha = file_sha256(gcode)
    lease = launch_state.acquire(_printer_key(state, job), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=observed_norm.get("filename"))
    # AMS mapping (#308): resolved inside the lease, against the fresh snapshot, BEFORE the
    # bed-clear approval is consumed — a job whose filaments cannot be mapped refuses with the
    # approval intact. A JOB.json ams_mapping is trusted verbatim; anything else is recomputed
    # from the snapshot's reported trays. A single-filament job that matches nothing reported
    # keeps the pre-#308 payload (source "legacy"); a multi-filament one refuses loudly here.
    from ..printers.bambulab import ams_mapping as _ams
    try:
        try:
            live_snapshot, _norm2 = ready_snapshot()  # again inside the lease: the first read may predate another start
        except RuntimeError as exc:
            raise refuse(exc) from exc
        try:
            ams_map, ams_source = _ams.resolve_for_print(
                Path(gcode).read_text(errors="ignore"), live_snapshot,
                job_mapping=(job.get("ams_mapping") if isinstance(job, dict) else None),
                dual=adapter.extruder_count() > 1)
        except _ams.MappingError as exc:
            raise refuse(exc) from exc
        if job_path is not None:
            _consume_approval(job_path, job, gcode, reprint=reprint)
    except BaseException:
        lease.release()
        raise
    lease.submitting()
    try:
        client.upload(package, remote)
        # Claim the start (#460): the Bambu listener's sink must not post a second start for this print.
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        client.start_print(remote, ams_mapping=ams_map)
    except BaseException as exc:
        _submission_failed(lease, remote, "Bambu Lab", exc)
    # Accepted is not proof of printing. No resubmission on delayed readback: the printer is polled
    # over the confirmation window (#271) and the lease stays uncertain if it never reports this file.
    window_s = _bambu_start_confirm_window()
    confirmed, live, snapshots = _confirm_bambu_start(client, adapter, remote, job_path, window_s)
    poll = StartPoll(confirmed, False, live, snapshots, window_s)
    out = _settle_polled_start(
        lease, poll, remote, method="bambulab_lan",
        uncertain_detail=("submitted, awaiting confirmation; printer reported state "
                          f"{live.get('state') or 'unknown'} ({live.get('gcode_state') or 'no gcode_state'}), "
                          f"file {live.get('filename') or 'none'!r} on the last of {snapshots} snapshot(s) "
                          f"over {window_s:g}s"),
        ams_mapping=ams_map, ams_mapping_source=ams_source,
        confirmation={"snapshots": snapshots, "window_s": window_s})
    if confirmed:
        try:
            out["record_path"] = record_start(gcode, remote)
        except Exception as exc:
            print(f"WARNING: start record failed: {exc}")
        # Camera parity (#294): a monitor watches this launched print — it makes
        # the completion post with the final camera image, runs the camera
        # watchdog, and posts this print's pause alerts (#460). The Bambu
        # listener domain covers prints started outside Print Handy; a
        # monitor that cannot start never fails the print.
        try:
            from print_handy.hermes import bambu_watch
            bambu_watch.start_monitor(adapter, remote)
        except Exception as exc:
            print(f"WARNING: camera monitor did not start: {exc}")
    _announce_polled_outcome(out)
    return out


def _print_elegoo_sdcp(adapter, state, gcode, job, job_path, wait_s, reprint):
    """The SDCP print path (#243), mirroring _print_prusa's guard order.

    Every check that can refuse cleanly runs before the submission lease: SDCP configured,
    live status idle with no readiness problems, G-code verified, single-tool only. Inside
    the lease the status is read again and the bed-clear approval is consumed; only then does
    anything reach the printer: the verified chunked-HTTP upload (the client read the size
    back), then the SDCP start command for that exact file. Acceptance is confirmed from a
    fresh status answer, never assumed; an unconfirmed start is uncertain and is never
    resubmitted. SDCP has no authentication, so nothing here can add any.
    """
    def refuse(exc):
        return SystemExit(f"REFUSED: {' '.join(str(exc).split())}. Nothing uploaded or started.")

    try:
        client = adapter.client()
    except RuntimeError as exc:
        raise refuse(exc) from exc

    def ready_status():
        raw = client.status()
        problems = adapter.readiness_problems(state, raw)
        norm = adapter.normalize_status(raw)
        if norm["state"] not in ("standby", "complete"):
            problems.append(f"printer state is {norm['state']!r}, not idle")
        if problems:
            raise RuntimeError("; ".join(dict.fromkeys(problems)))
        return raw, norm

    try:
        _raw, observed_norm = ready_status()
        verify_gcode(gcode)
        _refuse_multicolor(gcode.read_text(errors="ignore"))
    except RuntimeError as exc:
        raise refuse(exc) from exc
    sha = file_sha256(gcode)
    remote = remote_name(gcode, sha, job)
    lease = launch_state.acquire(_printer_key(state, job), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=observed_norm.get("filename"))
    try:
        try:
            ready_status()  # again inside the lease
        except RuntimeError as exc:
            raise refuse(exc) from exc
        if job_path is not None:
            _consume_approval(job_path, job, gcode, reprint=reprint)
    except BaseException:
        lease.release()
        raise
    lease.submitting()
    try:
        client.upload(gcode, remote)
        # Claim the start (#460): the family listener must not post a second start for this print.
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        client.start_print(remote)
    except BaseException as exc:
        _submission_failed(lease, remote, "SDCP", exc)
    poll = _poll_start_confirmation(lambda: adapter.normalize_status(client.status()), remote,
                                    job_path=job_path, stage="sdcp_start_confirm")
    out = _settle_polled_start(lease, poll, remote, method="elegoo_sdcp")
    confirmed = out["started"]
    if confirmed:
        try:
            out["record_path"] = record_start(gcode, remote)
        except Exception as exc:
            print(f"WARNING: start record failed: {exc}")
        # Timelapse (#295): a monitor watches this launched print — the
        # print path starts it; it makes the completion post, posts this
        # print's pause alerts (#460), and, once an SDCP camera source is
        # verified, captures frames for a timelapse attached to that post
        # (no source is verified today — printers/elegoo_sdcp/camera.py —
        # so for now the monitor is the completion post, with the reason
        # logged). Prints started outside Print Handy belong to the
        # elegoo_sdcp listener domain (family_event_listener). A monitor
        # that cannot start never fails the print.
        try:
            from print_handy.hermes import elegoo_watch
            elegoo_watch.start_monitor(adapter, remote)
        except Exception as exc:
            print(f"WARNING: timelapse monitor did not start: {exc}")
    _announce_polled_outcome(out)
    return out


def _print_prusa(adapter, state, gcode, job, job_path, wait_s, reprint):
    """The PrusaLink print path (#242), mirroring _print_octoprint's guard order.

    Every check that can refuse cleanly runs before the submission lease: link configured,
    live snapshot idle with no readiness problems, G-code verified, tools within the model's
    toolheads. Inside
    the lease the snapshot is taken again and the bed-clear approval is consumed; only then
    does anything reach the printer: the verified upload (PUT, size read back by the client),
    then the POST that starts that exact file. Acceptance is confirmed from a fresh snapshot,
    never assumed; an unconfirmed start is uncertain and is never resubmitted.
    """
    def refuse(exc):
        return SystemExit(f"REFUSED: {' '.join(str(exc).split())}. Nothing uploaded or started.")

    try:
        client = adapter.client()
    except RuntimeError as exc:
        raise refuse(exc) from exc

    def ready_snapshot():
        snapshot = client.snapshot()
        problems = adapter.readiness_problems(state, snapshot)
        norm = adapter.normalize_status(snapshot)
        if norm["state"] not in ("standby", "complete"):
            problems.append(f"printer state is {norm['state']!r}, not idle")
        if problems:
            raise RuntimeError("; ".join(dict.fromkeys(problems)))
        return snapshot, norm

    try:
        _snapshot, observed_norm = ready_snapshot()
        verify_gcode(gcode)
        _refuse_multicolor(gcode.read_text(errors="ignore"))
    except RuntimeError as exc:
        raise refuse(exc) from exc
    sha = file_sha256(gcode)
    remote = remote_name(gcode, sha, job)
    lease = launch_state.acquire(_printer_key(state, job), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=observed_norm.get("filename"))
    try:
        try:
            ready_snapshot()  # again inside the lease
        except RuntimeError as exc:
            raise refuse(exc) from exc
        if job_path is not None:
            _consume_approval(job_path, job, gcode, reprint=reprint)
    except BaseException:
        lease.release()
        raise
    lease.submitting()
    try:
        client.upload(gcode, remote)
        # Claim the start (#460): the family listener must not post a second start for this print.
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        client.start_print(remote)
    except BaseException as exc:
        _submission_failed(lease, remote, "PrusaLink", exc)
    poll = _poll_start_confirmation(lambda: adapter.normalize_status(client.snapshot()), remote,
                                    job_path=job_path, stage="prusalink_start_confirm")
    out = _settle_polled_start(lease, poll, remote, method="prusalink")
    confirmed = out["started"]
    if confirmed:
        try:
            out["record_path"] = record_start(gcode, remote)
        except Exception as exc:
            print(f"WARNING: start record failed: {exc}")
        # Timelapse (#295): a monitor watches this launched print — the
        # print path starts it; it makes the completion post, posts this
        # print's pause alerts (#460), and, when the printer registers a
        # camera with PrusaLink, captures frames for a timelapse attached
        # to that post. Prints started outside Print Handy belong to the
        # prusalink listener domain (family_event_listener). A monitor
        # that cannot start never fails the print.
        try:
            from print_handy.hermes import prusa_watch
            prusa_watch.start_monitor(adapter, remote)
        except Exception as exc:
            print(f"WARNING: timelapse monitor did not start: {exc}")
    _announce_polled_outcome(out)
    return out


def _print_flashforge_stock(adapter, state, gcode, job, job_path, wait_s, reprint):
    """The stock Flashforge LAN path, mirroring _print_bambu's guard order.

    Every check that can refuse cleanly runs before the submission lease: host and
    credentials present, G-code free of Klipper or ZMod commands, /detail idle, then
    the rest of the G-code check. Inside the lease the snapshot is taken again, the bed-clear approval is
    consumed, and only then does anything reach the printer: one upload with print
    disabled, then /printGcode for that exact name. Upload and start are one uncertain
    step from submitting() on. Acceptance is confirmed from a fresh /detail, never assumed.
    """
    def refuse(exc):
        return SystemExit(f"REFUSED: {' '.join(str(exc).split())}. Nothing uploaded or started.")

    from ..printers.flashforge_stock.client import StockLanError
    try:
        client = adapter.client()
    except (RuntimeError, StockLanError) as exc:
        raise refuse(exc) from exc

    def selected_slot():
        selection = job.get("filament_selection") if isinstance(job, dict) else None
        if not isinstance(selection, dict) or "slot" not in selection:
            raise RuntimeError("pick one IFS slot before a stock Flashforge print")
        slot = selection["slot"]
        if type(slot) is not int or not 1 <= slot <= adapter.slot_count:
            raise RuntimeError(f"IFS slot must be an integer from 1 through {adapter.slot_count}")
        return slot

    def ready_snapshot():
        detail = client.detail()
        problems = adapter.readiness_problems(state, detail)
        reduced = adapter.normalize_status(detail)
        if reduced["state"] not in ("standby", "complete", "cancelled"):
            problems.append(f"printer state is {reduced['state']!r}, not idle")
        try:
            occupied = client.slots_of(detail)
        except StockLanError as exc:
            problems.append(str(exc))
            occupied = {}
        slot = selected_slot()
        if occupied.get(slot) is not True:
            problems.append(f"IFS slot {slot} does not report filament")
        if problems:
            raise RuntimeError("; ".join(dict.fromkeys(problems)))
        return reduced

    try:
        text = gcode.read_text(errors="ignore")
        problems = adapter.gcode_problems(text)
        if problems:
            raise RuntimeError(problems[0])
        observed = ready_snapshot()
        verify_gcode(gcode)
        adapter.normalize_tool_mapping({str(t): t + 1 for t in sorted(_gcode_tools(text) | {0})}, state)
    except RuntimeError as exc:
        raise refuse(exc) from exc
    sha = file_sha256(gcode)
    remote = remote_name(gcode, sha, job)
    if not remote.endswith(".gcode"):
        remote += ".gcode"
    lease = launch_state.acquire(_printer_key(state, job), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=observed.get("filename"))
    try:
        try:
            ready_snapshot()  # again inside the lease: the first read may predate another start
        except RuntimeError as exc:
            raise refuse(exc) from exc
        if job_path is not None:
            _consume_approval(job_path, job, gcode, reprint=reprint)
    except BaseException:
        lease.release()
        raise
    lease.submitting()
    try:
        client.upload(gcode, remote)
        # Claim the start (#460): the family listener must not post a second start for this print.
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        client.print_gcode(remote)
    except BaseException as exc:
        _submission_failed(lease, remote, "stock Flashforge", exc)
    poll = _poll_start_confirmation(lambda: adapter.normalize_status(client.detail()), remote,
                                    job_path=job_path, stage="flashforge_stock_start_confirm")
    out = _settle_polled_start(lease, poll, remote, method="flashforge_stock")
    confirmed = out["started"]
    if confirmed:
        try:
            out["record_path"] = record_start(gcode, remote)
        except Exception as exc:
            print(f"WARNING: start record failed: {exc}")
        # Timelapse (#373): a monitor watches this launched print — the
        # print path starts it; it captures frames from the camera source
        # the printer advertises, makes the completion post with the
        # timelapse attached, and posts this print's pause alerts (#460).
        # Prints started outside Print Handy belong to the
        # flashforge_stock listener domain (family_event_listener). A
        # monitor that cannot start never fails the print.
        try:
            from print_handy.hermes import stock_watch
            stock_watch.start_monitor(adapter, remote)
        except Exception as exc:
            print(f"WARNING: timelapse monitor did not start: {exc}")
    _announce_polled_outcome(out)
    return out


def _require_idle(st):
    state,fn,busy=_printer_phase(st)
    if state == "printing" or busy == "Printing":
        raise SystemExit(f"REFUSED: printer is busy ({state}, {fn}). Nothing uploaded or started.")
    if st.get("display_off") == 0 and state not in ("standby", "complete", "cancelled", "error"):
        raise SystemExit(f"REFUSED: native printer state is {state!r} for {fn}; nothing uploaded or started.")
    return state,fn,busy


def _confirm_generic_start(st, remote, wait_s, lease):
    """Wait for the one accepted start to show as printing: (out, readback, state).

    Only a readback naming THIS file counts (launch_state.same_remote): printing, or preparing (lease
    released, claim kept: the start card is posted), or failed/cancelled (lease released, REFUSED-style
    SystemExit). Anything else, including a still-idle printer, a stale previous filename or an unreadable
    status, leaves the lease uncertain and frees the claim for the listener: after a 200 the printer may
    still be starting. `out` is None when printing. A readback exception propagates to the caller, which
    settles it as uncertain.
    """
    state = _wait_printing(st, remote, wait_s)
    readable, readback = _read_start_readback(st, remote, native=False)
    if state == "printing" or (readback and readback.outcome == "printing"):
        return None, readback, state
    if readback and readback.outcome == "preparing":
        lease.release()  # accepted; the busy printer now refuses another start
        return {"started": True, "accepted": True, "phase": "preparing", "outcome": "preparing", "file": remote,
                "method": "print/start", "message": readback.message}, readback, state
    if readback and readback.outcome == "failed":
        release_start_announcement(remote)
        lease.release()  # the printer reports this file's start failed
        raise SystemExit("The printer did not start the print, and it was not retried. "
                         "Check the printer screen before trying again." + _detail_tail(readback.message))
    lease.uncertain("submitted, awaiting confirmation")  # reconciled when the printer reports this file
    release_start_announcement(remote)
    return {"started": False, "accepted": True, "phase": "awaiting_confirmation", "outcome": "unconfirmed",
            "file": remote, "method": "print/start", "message": "submitted, awaiting confirmation"}, readback, state


def print_now(target, wait_s=25, accept_default_z_offset=False, reprint=False, remap_slot_num=None):
    """Bed is confirmed clear: verify, upload, start, confirm, record. One command."""
    if remap_slot_num is not None:
        remap_res = remap_slot(target, remap_slot_num, tool="0")
        print(f"SLOT REMAPPED: T0 -> slot {remap_res['slot']} ({remap_res['selection']['color']} {remap_res['selection']['material']})", flush=True)
    g=Path(resolve_print_target(target)).expanduser().resolve()
    job=None; job_path=None
    if g.name=="JOB.json":
        job_path=g; job=json.loads(g.read_text())
        if not job.get("gcode"):
            raise RuntimeError("stale color artifact: reslice current JOB before launch")
        g=Path(job["gcode"]).expanduser().resolve()
    else:
        # Slices live at <job>/sliced/orca/<uuid>/plate_1.gcode (four levels). Search every
        # nearby ancestor so an older slice is always bound to its JOB and the stale guard runs.
        # Stop at the temp root too. This profile sets TMPDIR inside itself, so a fixture
        # such as <scratch>/tmpXXX/x.gcode otherwise climbs into the real profile JOB.json.
        temp_root = Path(os.environ.get("TMPDIR") or "/tmp").resolve()
        for parent in list(g.parents)[:5]:
            if parent in (Path("/"), temp_root) or temp_root in parent.parents:
                break
            if (parent/"JOB.json").is_file():
                job_path=parent/"JOB.json"; job=json.loads(job_path.read_text()); break
    cur = Path(job["gcode"]).expanduser().resolve() if isinstance(job, dict) and job.get("gcode") else None
    if (cur and cur.parent != g.parent and cur.is_file() and isinstance(job, dict)
            and not job.get("derived_plates")):
        # A file from an earlier slice of this job (2026-10-10: the bot reran the pre-reslice name).
        raise SystemExit(f"REFUSED: {g.name} is an older slice of this job. The current slice is {cur}; "
                         f"run print-now {job_path} instead. Nothing uploaded or started.")
    require_current_artifact(job, g)
    if not g.is_file(): raise SystemExit(f"REFUSED: no G-code at {g}")
    # The job's printer (#334): a recorded printer_id resolves the roster entry, its adapter
    # and its hardware record; a record written before the upgrade has no id and prints on
    # the configured single printer, exactly as it always has.
    printer_id = job.get("printer_id") if isinstance(job, dict) else None
    if printer_id:
        from print_handy import settings as _pb_settings  # this function shadows `settings` locally below
        try:
            entry = _pb_settings.by_id(printer_id)
        except _pb_settings.SettingsError as exc:
            raise SystemExit(f"REFUSED: {exc}. Nothing uploaded or started.")
        have = printers.job_model(job)
        if have != entry["model"]:
            raise SystemExit(f"REFUSED: this job was prepared for {have}, but printer {printer_id!r} is an "
                             f"{entry['model']}. Nothing uploaded or started.")
        adapter = printers.get(entry["model"], _pb_settings.view_for(printer_id))
    else:
        try:
            if isinstance(job, dict): printers.check_job_model(job)
        except printers.JobModelMismatch as exc:
            raise SystemExit(f"REFUSED: {exc}. Reslice it for the configured printer; nothing uploaded or started.")
        adapter = printers.current()
    _timing(job_path, 'print_now_start')
    # accept_default_z_offset is kept for CLI compatibility only. With display_off=0 ZMod applies the
    # native screen zProbeOffset, which is the correct calibrated value; no Z_OFFSET in START_PRINT is fine.
    st=printer_state.load(printer_id) if printer_id else printer_state.load()
    if getattr(adapter, 'transport', None) == 'octoprint':
        return _print_octoprint(adapter, st, g, job, job_path, wait_s, reprint)
    if getattr(adapter, 'transport', None) == 'flashforge_stock':
        return _print_flashforge_stock(adapter, st, g, job, job_path, wait_s, reprint)
    if getattr(adapter, 'transport', None) == 'prusalink':
        from . import delivery as _delivery
        if 'printer' in _delivery.methods():
            return _print_prusa(adapter, st, g, job, job_path, wait_s, reprint)
        try:
            verify_gcode(g)
        except RuntimeError as exc:
            raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
        raise SystemExit(f"REFUSED: {adapter.label} print delivery is not enabled in Print Handy (add "
                         f"'printer' to [delivery] methods to print over PrusaLink; the link status alone is "
                         f"read-only). {g} passed offline checks; move the file yourself (not yet tested on "
                         "a printer). Nothing uploaded or started.")
    if getattr(adapter, 'transport', None) == 'elegoo_sdcp':
        from . import delivery as _delivery
        if 'printer' in _delivery.methods():
            return _print_elegoo_sdcp(adapter, st, g, job, job_path, wait_s, reprint)
        try:
            verify_gcode(g)
        except RuntimeError as exc:
            raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
        raise SystemExit(f"REFUSED: {adapter.label} print delivery is not enabled in Print Handy (add "
                         f"'printer' to [delivery] methods to print over SDCP; status alone is "
                         f"read-only). {g} passed offline checks; move the file yourself (not yet tested on "
                        "a printer). Nothing uploaded or started.")
    if getattr(adapter, 'transport', None) == 'bambulab_lan':
        from . import delivery as _delivery
        if 'printer' in _delivery.methods():
            return _print_bambu(adapter, st, g, job, job_path, wait_s, reprint)
        # Print delivery is not enabled for this printer: verify the file for manual handoff;
        # the bed-clear approval is left unused, exactly as for the offline path below.
        try:
            verify_gcode(g)
        except RuntimeError as exc:
            raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
        raise SystemExit(f"REFUSED: {adapter.label} print delivery is not enabled in Print Handy (add "
                         f"'printer' to [delivery] methods to print over LAN; LAN status alone is "
                         f"read-only). {g} passed offline checks; use the Bambu package delivery and "
                         "move the file yourself (not yet tested on a printer). Nothing uploaded or started.")
    if adapter.connection == 'offline':
        # No connection exists. Verify the file for manual handoff; the bed-clear approval is left unused.
        try:
            verify_gcode(g)
        except RuntimeError as exc:
            raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
        raise SystemExit(f"REFUSED: {adapter.label} has no live connection in Print Handy (no upload, start or "
                         f"control). {g} passed offline checks; use the Bambu package delivery and move the file "
                         "yourself (not yet tested on a printer). Nothing uploaded or started.")
    state,fn,busy=_require_idle(st)
    mesh=mesh_readiness(st)
    if mesh["problem"]:
        raise SystemExit(f"REFUSED: {mesh['problem']}. Nothing uploaded or started.")
    try:
        # Pre-upload checks. Report them as one REFUSED line so print_next releases the plate's
        # claim and the operator can retry after fixing the cause (a traceback kept it held).
        verify_gcode(g)
        selection = job.get("filament_selection") if isinstance(job, dict) else None
        _timing(job_path, 'preflight_verify_done')
        if adapter.multi_slot:
            embedded_mapping = adapter.native.embedded_mapping
            normalize_mapping = adapter.native.normalize_mapping
            raw_mapping = embedded_mapping(g.read_text(), st)
            mapping = normalize_mapping(raw_mapping, st) if raw_mapping is not None else None
        else:
            _refuse_multicolor(g.read_text())
            mapping = None
    except (RuntimeError, MultiColorUnsupported) as exc:
        raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
    if mapping is not None:
        return print_mapped_native(g, st, mesh, mapping, job, job_path, wait_s, reprint)
    try:
        _verify_filament_readiness(st, selection)
    except RuntimeError as exc:
        if adapter.multi_slot: raise
        raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
    sha=file_sha256(g); remote=remote_name(g, sha, job)
    text=g.read_text(errors="ignore")
    if mesh.get('display_off') in (0, 1):
        try:
            validate_saved_mesh_start(text, mesh)
        except RuntimeError as exc:
            raise SystemExit(f'REFUSED: {exc}. Reslice with the reviewed machine profile; nothing uploaded.') from exc
    if mesh.get('display_off') == 0:
        try:
            validate_native_start(read_stock_detail(st), selection)
        except Exception as exc:
            raise SystemExit(f'REFUSED: native preflight: {exc}. Nothing uploaded or started.') from exc
    upload_path, upload_sha, remote, delivery = _zmod_upload_artifact(adapter, st, g, sha, remote)
    lease=launch_state.acquire(_printer_key(st, job), job_json=job_path, gcode_sha256=sha, remote=remote, observed_file=fn,
                               started_files=lambda: _moonraker_started_files(st))
    try:
        _require_idle(st)  # again inside the lease: the first read may predate another job's start
        if job_path is not None: _consume_approval(job_path, job, g, reprint=reprint)
        _timing(job_path, 'upload_begin')
        r=subprocess.run([str(MOONRAKERCTL),"upload",str(upload_path),remote,upload_sha],text=True,capture_output=True,timeout=300,
                         env=_moonrakerctl_env(st))
        if r.returncode: raise SystemExit("The file could not be uploaded to the printer, so nothing started. "
                                          "Check the printer is on and reachable, then try again."
                                          + _detail_tail(r.stderr or r.stdout))
        try:
            up_json=json.loads(r.stdout)
        except Exception as e:
            raise SystemExit("The printer's reply to the upload could not be understood, so nothing started. "
                             "Try again; if it keeps happening, check the printer screen."
                             + _detail_tail(str(e)))
        res=up_json.get("result", up_json) if isinstance(up_json, dict) else {}
        item=res.get("item") if isinstance(res, dict) else None
        if not isinstance(item, dict) or item.get("root")!="gcodes" or item.get("path")!=remote:
            raise SystemExit("The printer did not confirm the uploaded file landed where it should, so nothing started. "
                             "Try again; if it keeps happening, check the printer screen."
                             + _detail_tail(json.dumps(up_json)[:300]))
        # Moonraker verified the SHA256 sent with the upload (mismatch = HTTP 400 = the upload failure above).
        _timing(job_path, 'upload_verified')
        if delivery: _note_delivery_format(job, job_path, delivery)
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        _announce_print_name(st, job, gcode=g)
        _publish_job_card(st, job, remote, text)
    except BaseException:
        lease.release()  # nothing was sent to start
        release_start_announcement(remote)  # the claim was taken in the block above
        raise
    # Baseline for the lost-response reconcile: heater targets seen later prove this start only if every
    # heater was off before it. Read-only; unreadable counts as not proven.
    heaters_were_off = _preparation_evidence(st) is False
    lease.submitting()
    sent = 0
    try:
        for command in adapter.moonraker_start_commands(remote):
            _mr(st, command["path"], data=command["body"])
            sent += 1
    except BaseException as exc:
        if not isinstance(exc, Exception):
            lease.uncertain(f"start request did not complete: {exc!r}")
            raise
        _timing(job_path, 'start_response_lost', detail=type(exc).__name__)
        # The 30 s POST can time out after Moonraker accepted it, like the mapped native route's:
        # reconcile from the printer (exact file, else heater evidence, else pending). An HTTP 4xx
        # answer to the first command is a refusal and ends as REFUSED with the printer's message.
        out, rejection = _settle_lost_start(lease, st, remote, exc, heaters_were_off=heaters_were_off,
                                            first_command=sent == 0, native=False,
                                            base={"file": remote, "method": "print/start"})
        if out is None:
            raise SystemExit(_rejected_start_message(rejection)) from exc
        if out["accepted"]:
            try:
                out["record_path"] = str(record_start(g, remote))
            except Exception as record_exc:
                print(f"WARNING: print start record failed: {record_exc}")
        print("START SUBMITTED; AWAITING CONFIRMATION " + json.dumps(out), flush=True)
        _emit_start_outcome(out)
        return out
    _timing(job_path, 'start_submitted')
    print("START REQUEST ACCEPTED " + json.dumps({"file": remote, "printing_confirmed": False}), flush=True)
    method="print/start"
    try:
        out, readback, state = _confirm_generic_start(st, remote, wait_s, lease)
    except Exception as exc:
        # The request is out: a transient 503 or a Klippy restart while reading back is neither a
        # traceback nor a refusal. Uncertain, never resubmitted, claim freed for the listener.
        return _uncertain_after_submission(lease, remote, exc, method=method)
    if out is not None:
        print("START SUBMITTED; AWAITING CONFIRMATION " + json.dumps(out), flush=True)
        _emit_start_outcome(out)
        return out
    lease.release()  # printing; the busy printer now refuses another start
    record_path=None
    try: record_path=record_start(g, remote)
    except Exception as e: print(f"WARNING: print start record failed: {e}")
    if not isinstance(record_path,(str,os.PathLike)): record_path=None
    elif record_path is not None: record_path=str(record_path)
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    est=gcode_estimate_s(text); fg=_filament_total_g(text)
    warm=5*60; now=datetime.now(ZoneInfo(TIMEZONE))
    title=(job or {}).get("title") or g.stem
    try: print_no=_assign_print_number(job_path)
    except Exception as e: print_no=None; print(f"WARNING: could not assign a print number: {e}")
    out={"started":True,"title":title,"print_number":print_no,"file":remote,"method":method,
         "phase":"executing","outcome":"printing","message":(readback.message if readback else "printing"),
         "extrusion_confirmed":bool(readback and (readback.evidence or {}).get("extrusion_confirmed")),
         "print_time":human_dur(est) if est else None,"filament_g":round(fg) if fg is not None else None,
         "finish_et":(now+timedelta(seconds=(est or 0)+warm)).strftime("%-I:%M %p %a") if est else None}
    if delivery: out["delivery_format"]=delivery
    settings=start_print_settings(text); z_notice=z_offset_notice(text)
    try: hardware=st.get("printer","Klipper")+", "+printer_state.summary(st)
    except (KeyError, TypeError): hardware=None
    out["z_offset_notice"]=z_notice; out["record_path"]=record_path
    live=live_snapshot(st); out["live"]=live
    screen_z=mesh.get("z_offset_mm"); out["screen_z_offset_mm"]=screen_z; out["native_screen_z_offset_mm"]=screen_z_offset(st); out["bed_mesh"]=mesh
    out["details"]=job_details(out, hardware=st, selection=selection, settings=settings,
                               record_path=record_path, z_notice=z_notice, live=live,
                               screen_z=screen_z, mesh=mesh, printer=st.get("printer") or (None if adapter.multi_slot else adapter.label),
                               nozzle_mm=(st.get("nozzle") or {}).get("diameter_mm"), calibration=adapter.has_native_screen)
    print("PRINT STARTED " + json.dumps(out))
    print(out["details"])
    _emit_start_outcome(out)  # last, so a short tail always carries the phase
    return out


def _print_now_cli(target, **kw):
    """print-now with an honest outcome line. A failure before any lease reached submitting()
    never asked the printer to start, so it ends as a REFUSED line (the chat then says it did
    not start and the one-start claim is released); after submitting() it stays uncertain."""
    import traceback
    launch_state.reset_submitted()
    try:
        return print_now(target, **kw)
    except SystemExit:
        raise
    except Exception as exc:
        traceback.print_exc()  # the detail stays in the log and receipt
        reason = ' '.join(str(exc).split()) or type(exc).__name__
        if launch_state.submitted():
            raise SystemExit(f"UNCERTAIN: the start was sent and then failed: {reason}") from exc
        raise SystemExit(f"REFUSED: {reason}. Nothing started.") from exc


def main():
    try:
        _main()
    except MultiColorUnsupported as exc:
        raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing sliced, uploaded or started.")


def _main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("url")
    f.add_argument("--intake-id", help="fresh Discord request identity; isolates history")
    f.add_argument("--inspect-only", action="store_true", help="download selected geometry and audit without slicing approval or slicing")
    f.add_argument("--select", type=int)
    f.add_argument("--approved", action="store_true", help="the operator approved exactly the selected source and any supports")
    f.add_argument("--brim", type=int, default=None, help="override brim width mm (e.g. 0 to disable)")
    f.add_argument("--printer", choices=printers.MODELS, help="printer model this job is prepared for (default: [printer] model)")
    f.add_argument("--filament-slot", type=int, help="AD5X IFS feeder slot (1-4)")
    f.add_argument("--filament-color", type=str, help="filament color matching IFS slot")
    f.add_argument("--filament-material", type=str, help="filament material matching IFS slot")
    f.add_argument("--filament-spoolman-id", "--spoolman-id", dest="filament_spoolman_id", type=int, default=None, help="Spoolman spool ID")
    s = sub.add_parser("slice")
    s.add_argument("job_json")
    s.add_argument("--approved", action="store_true")
    s.add_argument("--printer", choices=printers.MODELS, help="printer model to slice this job for (recorded on the job)")
    s.add_argument("--brim", type=int, default=None, help="override brim width mm (e.g. 0 to disable)")
    s.add_argument("--separate", action="store_true", help="multi-plate source: slice one G-code per plate instead of merging all parts onto one bed")
    filament_select=sub.add_parser("filament-select", help="record a user-confirmed slot/color choice in a model JOB.json")
    filament_select.add_argument("job_json")
    filament_select.add_argument("--slot", type=int, required=True)
    filament_select.add_argument("--color", required=True)
    filament_select.add_argument("--material", required=True)
    filament_select.add_argument("--spoolman-id", type=int, required=True)
    unit_select = sub.add_parser("unit-select", help="record a user-confirmed inch vs mm choice for a small STL in a model JOB.json; no slicing or start")
    unit_select.add_argument("job_json")
    unit_select.add_argument("--units", choices=["mm", "inch"], required=True)
    cp = sub.add_parser('color-plan', help='offline per-part and source-filament selection questions')
    cp.add_argument('job_json')
    cs = sub.add_parser('color-select', help='save source-bound color decisions; no slicing or start')
    cs.add_argument('job_json')
    cs.add_argument('decisions_json')
    filament_status = sub.add_parser("filament-status", help="read-only labeled AD5X filament telemetry")
    final=sub.add_parser("finalize-gcode", help="finalize reviewed multicolor G-code metadata and native checksum; no upload")
    final.add_argument("gcode")
    final.add_argument("--tool-map", required=True, help="JSON tool-to-physical-slot mapping")
    v=sub.add_parser("verify-gcode"); v.add_argument("gcode")
    remap=sub.add_parser("remap-slot", help="remap single-color job or G-code tool to another physical IFS slot")
    remap.add_argument("target", help="JOB.json or plate.gcode")
    remap.add_argument("--slot", type=int, required=True, help="physical IFS slot (1-4)")
    remap.add_argument("--tool", type=str, default="0", help="logical tool index (default: 0)")
    pn=sub.add_parser("print-now",help="bed confirmed clear: verify, upload, start, confirm, record (G-code or JOB.json)"); pn.add_argument("target")
    pn.add_argument("--accept-spool-change",action="store_true",help="operator confirmed printing with the spool now in a slot that changed since slicing (same material family only)")
    pn.add_argument("--remap-slot", "--slot", dest="remap_slot", type=int, default=None, help="remap T0 to another physical IFS slot (1-4) without physical spool changes")
    for x in (pn,f,s):
        x.add_argument("--accept-default-z-offset",action="store_true",help="the operator accepted the printer default Z offset for this start")
        x.add_argument("--reprint",action="store_true",help="the operator gave a NEW bed-clear approval to reprint the same G-code")
    rl=sub.add_parser("reconcile-launch",help="show, or after checking the printer settle, an uncertain start; never resubmits")
    outcome=rl.add_mutually_exclusive_group()
    outcome.add_argument("--started",action="store_true",help="the operator saw that start reach the printer")
    outcome.add_argument("--not-started",action="store_true",help="the operator saw that the printer never started it")
    rt=sub.add_parser("routine-receipt",help="immediate typical-duration receipt for a calibration routine (no printer contact)"); rt.add_argument("routine",choices=sorted(ROUTINE_TYPICAL_MIN))
    sub.add_parser("routine-status",help="read-only accepted/running/completed phase from Klipper idle_timeout")
    for x in (f,s):
        x.add_argument("--print-now",action="store_true",help="bed confirmed clear: start the print right after slicing")
        x.add_argument('--units', choices=['mm', 'inch'], default=None, help='user-confirmed units for a small STL (largest dimension under 10 mm); required before slicing one')
        x.add_argument('--color-mode', choices=['per_part', 'single_color'])
        x.add_argument('--color-slot', type=int, action='append', help='slot for each displayed part/filament question, in order')
    ma=sub.add_parser("audit",help="read-only mesh topology audit; no repair or slicing")
    ma.add_argument("source")
    lay = sub.add_parser("layout", help="offline 100%-scale geometry inspection, arrangement, and export; no slicing or start")
    lay.add_argument("source")
    lay.add_argument("-o", "--outdir", default=None, help="directory for arranged 3MF/STL/PNG/JSON outputs")
    lay.add_argument("--inspect-only", action="store_true", help="inspect geometry and nominal bed fit without writing arranged files")
    lay.add_argument("--bed", nargs=3, type=float, default=[220.0, 220.0, 220.0], metavar=("X", "Y", "Z"))
    lay.add_argument("--margin", type=float, default=5.0)
    lay.add_argument("--gap", type=float, default=5.0)
    lay.add_argument("--input-unit", default=None, help="explicit length unit for STL or unitless model (mm, cm, m, inch, ft)")
    lay.add_argument("--split-stl-components", action="store_true", help="treat disconnected STL shells as separate parts")
    lay.add_argument("--allow-rotations", nargs="*", type=float, default=None, help="candidate Z rotations in degrees")
    cad = sub.add_parser("cad-split", help="offline opt-in reviewed CAD split plan and printable dowel preparation; no slicing or start")
    cad.add_argument("source", help="input .stl path")
    cad.add_argument("-o", "--outdir", default=None, help="output directory for split artifacts and split_validation.json")
    cad.add_argument("--plan", required=True, help="reviewed split plan preset (rack_adapter_10to19, macmini_mount_a1347) or JSON path")
    cad.add_argument("--execute", action="store_true", help="invoke OpenSCAD CLI to render boolean split halves and verify volume conservation")
    m=sub.add_parser("plate-manifest",help="read-only source plate inventory; does not authorize slicing")
    m.add_argument("source_3mf")
    r=sub.add_parser("record-start",help="after a confirmed print start: write the JSON print record"); r.add_argument("gcode"); r.add_argument("--moonraker-name"); r.add_argument("--started-at",help="ISO UTC, default now")
    prep = sub.add_parser("prepare-first-layer", help="offline: generate and slice a one-layer test; never upload or start")
    prep.add_argument("--slot", type=int, required=True)
    prep.add_argument("--color", required=True)
    prep.add_argument("--material", required=True)
    prep.add_argument("--spoolman-id", type=int, required=True)
    prep.add_argument("--slicer", choices=["orca"], default="orca", help="canonical Orca engine; no legacy fallback")
    a=ap.parse_args();
    if a.cmd == 'unit-select':
        intake_unit_confirmation(a.job_json, units=a.units)
        saved = json.loads(Path(a.job_json).read_text())
        print('UNIT PROVENANCE RECORDED ' + json.dumps(saved.get('unit_decision') or saved.get('unit_provenance')))
        return
    if a.cmd in ('color-plan', 'color-select'):
        from .source_colors import inventory, prompts, validate_decisions
        path = Path(a.job_json)
        job = json.loads(path.read_text())
        manifest = inventory(job['downloaded_file'])
        job['color_manifest'], job['color_prompts'] = manifest, prompts(manifest)
        if a.cmd == 'color-select':
            set_color_decisions(job, validate_decisions(manifest, json.loads(Path(a.decisions_json).read_text()),
                                                        printer_state.load(), resolve_filament_selection))
        write_job(path.parent, job)
        print(json.dumps({'manifest': manifest, 'questions': prompts(manifest),
                          'decisions': job.get('color_decisions')}, indent=2))
        return
    if a.cmd=="audit":
        print(json.dumps(mesh_audit(a.source), indent=2)); return
    if a.cmd=="layout":
        from .geometry_layout import arrange_and_export, inspect_geometry
        src_path = Path(a.source)
        if a.inspect_only:
            res = inspect_geometry(
                src_path,
                bed=tuple(a.bed),
                margin=a.margin,
                input_unit=a.input_unit,
                split_stl=a.split_stl_components,
            )
            if a.outdir:
                out_dir = Path(a.outdir)
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "inspection.json").write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(res, indent=2))
            return
        out_dir = Path(a.outdir) if a.outdir else (src_path.parent / f"{src_path.stem}_layout_100pct")
        res = arrange_and_export(
            src_path,
            out_dir,
            bed=tuple(a.bed),
            margin=a.margin,
            gap=a.gap,
            angles=a.allow_rotations,
            input_unit=a.input_unit,
            split_stl=a.split_stl_components,
        )
        print(json.dumps(res, indent=2))
        return
    if a.cmd=="cad-split":
        from .cad_split import prepare_cad_split
        src_path = Path(a.source)
        out_dir = Path(a.outdir) if a.outdir else (src_path.parent / f"{src_path.stem}_cad_split")
        res = prepare_cad_split(src_path, out_dir, a.plan, execute_openscad=bool(a.execute))
        print(json.dumps(res, indent=2))
        return
    if a.cmd=="prepare-first-layer":
        prepare_first_layer(a.slot, a.color, a.material, a.spoolman_id, slicer=a.slicer); return
    if a.cmd=="plate-manifest":
        print(json.dumps(plate_manifest(a.source_3mf),indent=2)); return
    if a.cmd=="record-start":
        record_start(a.gcode, a.moonraker_name, a.started_at); return
    if a.cmd=="filament-select":
        selection = resolve_filament_selection(printer_state.load(), vars(a))
        selection = save_filament_selection(a.job_json, selection)
        print("FILAMENT SELECTION RECORDED " + json.dumps(selection))
        return
    if a.cmd=="finalize-gcode":
        finalize = printers.current().native.finalize
        st = printer_state.load()
        ok, probs, facts = printer_state.check(st)
        _print_check_warnings(facts)
        if not ok:
            raise RuntimeError('the printer check did not pass, so the G-code was not finalized: ' + '; '.join(probs))
        path = Path(a.gcode)
        backup = path.with_suffix(path.suffix + '.before-finalize')
        if not backup.exists():
            shutil.copy2(path, backup)
        sha = finalize(path, None, st, json.loads(Path(a.tool_map).read_text()))
        verify_gcode(path)
        print('FINALIZED ' + json.dumps({'path': str(path), 'sha256': sha, 'backup': str(backup)}))
        return
    if a.cmd=="verify-gcode":
        verify_gcode(a.gcode); return
    if a.cmd=="filament-status":
        st = printer_state.load()
        print(json.dumps(read_filament_telemetry(st), indent=2))
        return
    if a.cmd=="reconcile-launch":
        key=_printer_key(printer_state.load())
        if a.started or a.not_started:
            launch_state.resolve(key, "started" if a.started else "not_started")
        else:
            print(json.dumps(launch_state.pending(key), indent=2))
        return
    if a.cmd=="routine-receipt":
        print(routine_receipt(a.routine)); return
    if a.cmd=="routine-status":
        print(routine_phase(printer_state.load())); return
    pn_kw=dict(accept_default_z_offset=getattr(a,"accept_default_z_offset",False),reprint=getattr(a,"reprint",False),remap_slot_num=getattr(a,"remap_slot",None))
    if a.cmd=="remap-slot":
        res = remap_slot(a.target, a.slot, a.tool)
        print("SLOT REMAPPED " + json.dumps(res, indent=2))
        return
    if a.cmd=="print-now":
        if a.accept_spool_change:
            os.environ["PRINT_ACCEPT_SPOOL_CHANGE"] = "1"
            print("SPOOL CHANGE ACCEPTED: slots may hold a different spool than at slicing (same material family)", flush=True)
        _print_now_cli(a.target, **pn_kw); return
    st=printer_state.load()
    if a.cmd=="slice":
        if not a.approved: raise SystemExit("REFUSED: explicit slicing approval required")
        if a.printer:
            _job = json.loads(Path(a.job_json).read_text()); _job["printer_model"] = a.printer
            write_job(Path(a.job_json).parent, _job)
        if not intake_unit_confirmation(a.job_json, units=a.units, interactive=sys.stdin.isatty()):
            return
        if not intake_color_choices(a.job_json, st, a.color_mode, a.color_slot, sys.stdin.isatty()):
            return
        if a.separate:
            _job = json.loads(Path(a.job_json).read_text()); _job["plate_mode"] = "separate"
            write_job(Path(a.job_json).parent, _job)
        out=slice_job(a.job_json, brim_override=a.brim)
        if a.print_now: print_now(out, **pn_kw)
        return
    ok,probs,facts=printer_state.check(st)
    _print_check_warnings(facts)
    if not ok: raise SystemExit("I could not check the printer before starting, so nothing was sliced, uploaded or started. "
                                "Fix the check below, then try again:\n  " + "\n  ".join(probs))
    selection = None
    filament_specified = (a.filament_slot is not None or a.filament_color is not None or
                          a.filament_material is not None or a.filament_spoolman_id is not None)
    if filament_specified:
        try:
            selection = resolve_filament_selection(st, {
                "slot": a.filament_slot, "color": a.filament_color,
                "material": a.filament_material, "spoolman_id": a.filament_spoolman_id,
            })
        except ValueError as exc:
            raise SystemExit(f"That filament choice does not work: {exc} Nothing was sliced, uploaded or started.")
    job=inventory_for_url(a.url)
    if not job.get("sources"):
        raise SystemExit("REFUSED: source inventory contains no selectable files or profiles")
    if job.get("no_geometry_reason"):
        raise SystemExit(f"REFUSED: {job['no_geometry_reason']}")
    job["url"],job["hardware"],job["fetched_at"]=a.url,st,time.strftime("%Y-%m-%dT%H:%M:%S%z")
    stamp_job_printer(job)
    if a.printer: job["printer_model"]=a.printer
    for n,x in enumerate(job["sources"],1): x["n"]=n
    if selection is not None:
        job["filament_selection"] = selection
    folder=intake_folder(ARCHIVE, job["slug"], a.intake_id)
    if a.intake_id:
        job['intake_id'] = a.intake_id
        if (folder/'JOB.json').exists():
            previous = json.loads((folder/'JOB.json').read_text())
            if (previous.get('url') != a.url or previous.get('intake_id') != a.intake_id
                    or previous.get('downloaded_file') or previous.get('gcode')
                    or previous.get('color_decisions') or previous.get('approvals')):
                raise SystemExit('REFUSED: intake already has a prepared job; resume its exact JOB.json explicitly, never overwrite history')
    folder.mkdir(parents=True,exist_ok=True); write_job(folder,job); show(job)
    if a.select is None:
        print(f"\nSTOPPED: inventory saved at {folder/'JOB.json'}. Choose --select N; slicing requires --approved."); return
    if a.inspect_only and (a.approved or a.print_now):
        raise SystemExit('REFUSED: inspect-only cannot approve slicing or printing')
    if not a.approved and not a.inspect_only: raise SystemExit("REFUSED: --select requires the operator's explicit --approved slicing authorization; use --inspect-only for preparation")
    if a.select<1 or a.select>len(job["sources"]): raise SystemExit("invalid --select")
    # The explicit-selection gate guards the record-bound slot printers (AD5X pair):
    # the wrong record slot is the wrong filament, silently. A Bambu AMS model's
    # explicit choice is its job ams_mapping (or the print path's snapshot
    # resolution), never a record slot, so the gate does not apply to it (#671).
    _print_now_adapter = printers.get(job.get("printer_model") or printers.current().key)
    if a.print_now and _print_now_adapter.multi_slot and _print_now_adapter.slots_bound_by_record and not job.get("filament_selection"):
        raise SystemExit("REFUSED: --print-now requires explicit filament selection (--filament-slot, --filament-color, --filament-material, or use 'filament-select' command)")
    chosen=job["sources"][a.select-1]
    # Site profile settings are never imported. The selected 3MF/STL supplies geometry
    # only, and is resliced to the live verified local nozzle and spool. Preserve the
    # metadata as a warning so a PLA CF or 0.4 Bambu preset cannot masquerade as local
    # compatibility.
    if job["site"]=="makerworld":
        notes=[]
        if chosen.get("site_nozzle_mm") not in (None, st["nozzle"]["diameter_mm"]): notes.append(f"site profile declares {chosen['site_nozzle_mm']} mm")
        site_mats=set(chosen.get("materials") or [])
        if site_mats and st["spool"]["material"] not in site_mats: notes.append(f"site profile material is {', '.join(sorted(site_mats))}; loaded material is {st['spool']['material']}")
        if notes: print("SITE PROFILE NOT REUSED: " + "; ".join(notes) + ". Reslicing geometry to live local state.")
    srcdir=folder/"source"; path=download_for_site(job,chosen,srcdir)
    job["selected_source"],job["downloaded_file"],job["audit"]=chosen,str(path),audit(path, printer_model=job.get("printer_model") or printers.current().key); write_job(folder,job)
    print("\nAUDIT: "+json.dumps(job["audit"],indent=2))
    if a.inspect_only:
        print('SOURCE_INSPECTED ' + json.dumps({'job_json': str(folder/'JOB.json'), 'sliced': False, 'uploaded': False, 'started': False}))
        return
    if not intake_unit_confirmation(folder/'JOB.json', units=a.units, interactive=sys.stdin.isatty()):
        print(f"\nSTOPPED: unit confirmation pending at {folder/'JOB.json'}. Resume with slice --approved --units mm|inch; slicing requires --approved.")
        return
    if not intake_color_choices(folder/'JOB.json', st, a.color_mode, a.color_slot, sys.stdin.isatty()):
        return
    out=slice_job(folder/"JOB.json", brim_override=a.brim)
    if a.print_now: print_now(out, **pn_kw)
if __name__=="__main__": main()
