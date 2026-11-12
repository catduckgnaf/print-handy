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
import argparse, base64, contextvars, hashlib, html, http.client, ipaddress, json, logging, math, os, re, shutil, socket, subprocess, sys, time, urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, NamedTuple

from .. import firmware_update, printers, settings
from ..printers.base import MultiColorUnsupported
from . import _external, filament_color, gcode_3mf, launch_state, printer_state, purge, spoolman_client, start_recorder
from . import start_truth
from .filament_warnings import FilamentWarningPending
from .site_accounts import SiteLoginNeeded
from .threemf_canonical import intake_working_copy

logger = logging.getLogger(__name__)

from ..launch.print_now import (  # moved in 2.3.0 (#1323); old path kept for one release
    BAMBU_START_CONFIRM_S, BAMBU_START_POLL_S, INTAKE_RECEIPTS, MAP_PREFIX, START_CLAIMS,
    START_CLAIM_TTL_S, START_CONFIRM_DEFAULT_S, START_CONFIRM_POLL_S, START_OUTCOME_MARK,
    START_READBACK_QUERY, StartPoll, ToolRemapRefused, Z_OFFSET_NOTICE, _FILAMENT_COLOUR_RE,
    _OUTCOME_KEYS, _RECEIPT_STARTABLE_STATUSES, _announce_polled_outcome, _announce_print_name,
    _assign_print_number, _bambu_start_confirm_window, _bambu_total_estimated_time, _bang_lines,
    _baseline_heaters_off, _baseline_heats, _check_mapped_slot_color, _clear_active_spool,
    _confirm_bambu_start, _confirm_clock, _confirm_generic_start, _confirm_mapped_start,
    _confirm_sleep, _emit_mapped_outcome, _emit_start_outcome, _entries_after_watermark,
    _filament_total_g, _find_intake_receipt, _gcode_filament_colors, _gcode_store_entries,
    _gcode_store_watermark, _maintenance_detail_line, _moonraker_started_files,
    _note_delivery_format, _owning_job, _per_tool_filament_settings, _poll_start_confirmation,
    _preparation_evidence, _print_bambu, _print_elegoo_sdcp, _print_flashforge_stock,
    _print_now_control_lock, _print_now_locked, _print_octoprint, _print_prusa, _publish_job_card,
    _read_claims, _read_start_readback, _readback_status, _reconcile_lost_start,
    _record_intake_receipt_started, _rejected_start_message, _rerecord_selected_slot,
    _settle_intake_start_claim, _settle_lost_start, _settle_polled_start, _start_baseline,
    _start_confirm_window_s, _start_confirmed, _start_family_monitor, _start_pending,
    _start_receipt_error, _start_rejection, _start_visibly_running, _store_entry_mark,
    _submission_failed, _sync_active_spool, _uncertain_after_submission, _wait_printing,
    _write_checkpoint, _write_claims, _zmod_upload_artifact, _zmod_version,
    accepted_color_mismatches, accepted_label_mismatches, claim_start_announcement,
    color_mismatch_key, job_card, job_details, label_mismatch_key, live_snapshot, mesh_line,
    print_mapped_native, print_now, record_start, release_start_announcement, remap_slot,
    z_offset_notice,
)

from ..jobs.common import (  # moved in 2.3.0 (#1323); old path kept for one release
    ARCHIVE, BED_MM, FILAMENT_SLOTS, MAX_HEIGHT_MM, MOONRAKERCTL, PROFILE, STATE, TIMEZONE, UA,
    _moonrakerctl_env, _mr, request_json,
)


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


# What chat says for any Cults3D link (#712): Cults' API never shares files,
# so no credential can help. Said BEFORE any connect prompt, with no API-key
# walkthrough and no paths.
CULTS_REFUSAL = (
    "Cults3D doesn't let apps download its files. Download the .3mf or .stl from the "
    "model page in your browser and drop it here. Nothing was downloaded.")


class DownloadUnsupported(ValueError):
    """A site whose files no app can download (#712). ``cause`` is the short receipt error."""

    def __init__(self, message, cause):
        super().__init__(message)
        self.cause = cause


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
from . import model_sources as _model_sources
SUPPORTED_LINKS_MESSAGE = _model_sources.supported_links_message()


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


def _validated_public_ips(url):
    """The validated public addresses for a fetch URL, or raise DirectLinkError.

    Every address the host resolves to must be public; the caller connects
    to one of these addresses itself (see _open_once) rather than letting
    the transport resolve the name again."""
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
    return ips


def assert_public_url(url):
    """Refuse a fetch URL whose destination is not a public address.

    Runs before the first connection and again on every redirect target:
    a redirect to localhost or a private address is the standard way to
    smuggle one past a check on the original URL."""
    _validated_public_ips(url)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirects are followed manually so every hop re-runs assert_public_url."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PinnedConnection:
    """Dial one pre-validated address instead of re-resolving the host name.

    http.client takes the Host header — and, for HTTPS, the TLS server
    name and certificate check — from the host name the connection was
    built with. Only the socket target changes: _create_connection is
    pointed at the validated IP, so DNS is never consulted again between
    the public-address check and the connection."""

    def __init__(self, *args, _pinned_ip, **kwargs):
        super().__init__(*args, **kwargs)
        self._pinned_ip = str(_pinned_ip)
        self._create_connection = self._dial_pinned

    def _dial_pinned(self, address, timeout, source_address):
        return socket.create_connection(
            (self._pinned_ip, address[1]), timeout, source_address)


class _PinnedHTTPConnection(_PinnedConnection, http.client.HTTPConnection):
    pass


class _PinnedHTTPSConnection(_PinnedConnection, http.client.HTTPSConnection):
    pass


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_ip):
        super().__init__()
        self._pinned_ip = pinned_ip

    def http_open(self, req):
        pinned = self._pinned_ip
        return self.do_open(
            lambda host, **kwargs: _PinnedHTTPConnection(
                host, _pinned_ip=pinned, **kwargs),
            req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip):
        super().__init__()
        self._pinned_ip = pinned_ip

    def https_open(self, req):
        pinned = self._pinned_ip
        return self.do_open(
            lambda host, **kwargs: _PinnedHTTPSConnection(
                host, _pinned_ip=pinned, **kwargs),
            req, context=self._context)


def _open_once(url):
    """One GET with no redirect following (module-level so tests can fake the transport).

    The connection is pinned to an address from the resolution that was
    just validated (_validated_public_ips): the host name is never
    re-resolved at connect time, so a DNS answer that changes between
    the check and the connection (rebinding) cannot steer the fetch at
    a private address — on this hop or any redirect hop, since every hop
    goes through here. Environment proxies are disabled for the same
    reason: a proxy would resolve the name itself, outside the validated
    set. Each validated address is tried in turn before giving up."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last_error = None
    for ip in _validated_public_ips(url):
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
            _PinnedHTTPHandler(ip),
            _PinnedHTTPSHandler(ip))
        try:
            return opener.open(req, timeout=90)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc
    raise last_error


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
SITE_INVENTORY_FUNCS=_model_sources.inventory_funcs()
SITE_DOWNLOAD_FUNCS=_model_sources.download_funcs()
SITE_HOSTS=_model_sources.hosts_map()
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


from ..jobs.store import (  # moved in 2.3.0 (#1323); old path kept for one release
    CONTEXT, RECORD_KEYS, _detail_tail, _job_adapter, _job_for_gcode, _job_printer_id,
    _job_record_for_gcode, _printer_key, _recipe_changed, _recorded_printer_id, _timing,
    decisions_digest, effective_color_decisions, gcode_estimate_s, human_dur, remote_name,
    resolve_print_target, write_job,
)


from ..gcode.artifact import (  # moved in 2.3.0 (#1323); old path kept for one release
    _text_like_read_text, file_sha256,
)


from ..gcode.verify import (  # moved in 2.3.0 (#1323); old path kept for one release
    _TOOL_CANDIDATES, _TOOL_LINE_RE, _VERIFY_LINE_PREFIXES, _already_verified_artifact,
    _full_verified_marker, _gcode_tools, _print_check_warnings, _recorded_temperatures,
    _refuse_multicolor, _run_live_printer_check, _verification_facts_digest, _verify_lines,
    _verify_offline, _verify_routed, _verify_state, _with_gcode_temperatures,
    native_heater_lines_valid, sovol_style_start_matches, verify_gcode, verify_gcode_offline,
    zmod_md5_valid,
)


def finalize_multicolor(*args, **kwargs):
    """ZMod color/slot-map insertion and MD5, through the printer adapter. A printer without the
    ZMod contract (the stock AD5X, #658) gets nothing inserted: its launch binds the slot from the job."""
    native = printers.current().native_or_none
    return None if native is None else native.finalize(*args, **kwargs)




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
    try:
        tris = source_triangles(path)
    except RuntimeError as exc:
        # A file with no mesh at all (objects named, no triangles) is refused at intake like a
        # flat one (#194), not left to crash the fit check as an unexplained error (2026-11-03).
        if "no printable triangles" in str(exc):
            raise NoPrintableVolume(f"model has no printable geometry ({exc})") from exc
        raise
    if not tris:
        raise NoPrintableVolume("model has no printable geometry")
    from .mesh_fast import bounds_and_volume
    lo, hi, vol = bounds_and_volume(tris)
    if not all(math.isfinite(float(v)) for v in (*lo, *hi, vol)):
        # NaN/inf coordinates poison every extent, volume and fit check downstream; refuse at the
        # boundary with a message the operator can act on.
        raise NoPrintableVolume("model contains non-finite (NaN or infinite) coordinates; "
                                "re-export it from your CAD or slicer tool")
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
            out["objects"]=len(re.findall(r'<(?:\w+:)?object\b',text)); out["components"]=len(re.findall(r'<(?:\w+:)?component\b',text)); out["build_items"]=len(re.findall(r'<(?:\w+:)?item\b',text)); out["named_parts"]=re.findall(r'<metadata key="name" value="([^"]+)',text)[:30]
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

from ..gcode.bounds import (  # moved in 2.3.0 (#1323); old path kept for one release
    BOUNDS_CACHE, BOUNDS_CACHE_MAX_ENTRIES, BOUNDS_PARSER_VERSION, NUMBER,
    _BOUNDS_NATIVE_API_VERSION, _BOUNDS_NEVER_SWALLOW, _BOUNDS_SELF_CHECK_OK,
    _BOUNDS_SELF_CHECK_REFUSALS, _GCODE_FORBIDDEN_NAMES, _GCODE_G10_OFFSET_RE, _GCODE_G_FORBIDDEN,
    _GCODE_G_KNOWN, _GCODE_G_WORK_OFFSETS, _GCODE_LINE_RE, _GCODE_OFFSET_NAMES, _GCODE_STRAY_CR_RE,
    _GCODE_XYZ_RE, _GCODE_XYZ_RE_I, _bounds_impl_setting, _cached_bounds,
    _gcode_motion_bounds_python, _load_native_bounds, _native_bounds_result, _prune_bounds_cache,
    _reset_native_bounds_cache, cached_motion_bounds, gcode_motion_bounds,
)


from ..jobs.filament import (  # moved in 2.3.0 (#1323); old path kept for one release
    FILAMENT_SENSOR_OBJECTS, _validate_slot_selection, apply_stock_presence, configured_slots,
    position_limit, read_filament_telemetry, read_stock_detail, read_stock_filament_slots,
    read_stock_filament_slots_from_detail, resolve_filament_selection, telemetry_state,
    validate_filament_selection,
)


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


from ..launch.gates import (  # moved in 2.3.0 (#1323); old path kept for one release
    BED_CLEAR_REFUSAL, CHECKPOINT_NOT_SENT, IDLE_PRINT_STATES, MESH_PROFILE,
    READINESS_MAX_AGE_SECONDS, YOLO_WAIVED_MARK, _BED_CLEAR, _HEATER_ARG_RE, _HEATER_MACRO_RE,
    _HEATER_M_RE, _HEATER_SET_RE, _HEATER_VALUE_RE, _YOLO, _claim_bed_clear, _consume_approval,
    _gate, _gcode_heater_targets, _heater_limits, _native, _printer_phase, _refuse,
    _require_bed_clear, _require_idle, _require_idle_before_mapped_launch,
    _require_launch_checkpoint_clear, _verify_filament_readiness, _verify_readiness_adapter,
    _waive, check_filament_readiness, mesh_readiness, partial_job_refusal, refused_plates_digest,
    require_current_artifact, require_mechanical_limits, screen_z_offset, start_print_settings,
    validate_native_start, validate_saved_mesh_start, yolo_active,
)















































def retry_pending_spool_sync(filename, printer_id=None):
    """Retry a running job's launch-time pending active-spool sync (#838).

    At start, a Spoolman outage records `spoolman_usage_pending` on the job
    and the launch proceeds (#387) — but nothing read it back, so Moonraker's
    [spoolman] charged the previous print's spool for the whole run. The
    listener calls this after a successful Spoolman probe: when the current
    print's job carries an unresolved pending record naming a spool, point
    the active spool at it now and clear the record. Fail-soft by design:
    any failure leaves the record untouched and returns False; a multi-slot
    pending record (spool_id None — the stale-spool clear case) is not
    retried here, because no single spool is the job's.

    The retry acts on the JOB's printer: an explicit `printer_id` (the
    calling listener's roster identity) wins, else the job's recorded
    printer_id. Loading state without an ID reads the legacy/default
    printer's record, so a non-default printer's retry would otherwise
    point (or fail on) another printer's Moonraker endpoint (#847).
    """
    try:
        artifact = Path(resolve_print_target(filename)).expanduser().resolve()
        job, job_path = _job_record_for_gcode(artifact)
        pending = (job or {}).get('spoolman_usage_pending')
        if not isinstance(pending, dict) or not pending.get('spool_id'):
            return False
        slot = pending.get('slot')
        if slot not in (1, 2, 3, 4):
            return False
        routed = printer_id or _recorded_printer_id(job)
        st = printer_state.load(routed) if routed else printer_state.load()
        sync = (_sync_active_spool(st, slot, printer_id=routed) if routed
                else _sync_active_spool(st, slot))
        if sync.get('spoolman_pending') or sync.get('spool_id') != pending['spool_id']:
            return False
        job.pop('spoolman_usage_pending', None)
        write_job(job_path.parent, job)
        return True
    except (Exception, SystemExit) as exc:  # resolve_print_target refuses with SystemExit
        logger.warning('pending active-spool retry for %s left in place: %s', filename, exc)
        return False


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



def set_color_decisions(job, decisions):
    if decisions_digest(job.get('color_decisions')) != decisions_digest(decisions):
        if job.get('gcode'):
            job.setdefault('artifact_history', []).append({key: job[key] for key in
                ('gcode', 'gcode_verified', 'gcode_full_verified_sha256', 'provenance', 'artifact_binding',
                 'sliced_at', 'slice_config', 'slice_recipe') if key in job})
        for key in ('gcode', 'provenance', 'artifact_binding', 'sliced_at', 'slice_config', 'slice_recipe',
                    'gcode_full_verified_sha256'):
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
                ('gcode', 'gcode_verified', 'gcode_full_verified_sha256', 'provenance', 'artifact_binding',
                 'sliced_at', 'slice_config', 'slice_recipe') if key in job})
        for key in ('gcode', 'provenance', 'artifact_binding', 'sliced_at', 'slice_config', 'slice_recipe',
                    'gcode_full_verified_sha256'):
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




def _recipe_binding(job):
    """Recipe fields every artifact_binding carries; slice_job records the recipe before slicing."""
    return {'recipe_version': job['slice_recipe']['version'], 'recipe_sha256': job['slice_recipe']['sha256']}


def _placed_bounds_by_object(geometry, object_of_key):
    """Per-object placed bounds from a multicolor exporter report (P2-85).

    The union of the part rows' bounds and the cutter volumes' bounds. The
    merged-placement proof compares these against bounds computed from ALL
    of an object's authored volumes, so the landed side must include the
    cutters too: a negative volume reaching past its body otherwise reads
    as the export having landed somewhere else, and a sound slice is
    refused.
    """
    placed = {}

    def fold(oid, box):
        if oid in placed:
            prev = placed[oid]
            box = [(min(prev[a][0], box[a][0]), max(prev[a][1], box[a][1])) for a in range(3)]
        placed[oid] = box

    for row in geometry.get('parts', []):
        fold(object_of_key[row['part_key']], row['bounds_mm'])
    for oid, box in (geometry.get('cutter_bounds_by_object') or {}).items():
        fold(str(oid), box)
    return placed




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


def record_filament_warning_acknowledgement(folder_or_job, warnings=None, source_sha256=None,
                                            selected_plates=None, via='print_anyway'):
    """Record the operator's explicit acceptance of named filament warnings.

    Owner ruling (catduck, 2026-11-10): filament conditions warn and the
    user may accept the warning; they never hard-block a print. The
    acceptance is bound to the exact warning texts, the source hash and the
    selected plates it was given for — never a blanket waiver (the binding
    rule slice_warnings.record_unsupported_cantilever_override states for
    its record). With no explicit warnings, the job's
    filament_warnings_pending record (written by the gate that raised
    FilamentWarningPending) supplies them; with neither, there is nothing
    to accept and the call refuses rather than recording a blank waiver.
    The acceptance authorizes one slicing run: slice_job marks it used and
    the next slice_job consumes it, so a reslice presents the warnings
    again (the clearance pattern's reslice rule).
    """
    is_path = isinstance(folder_or_job, (str, Path))
    if is_path:
        p = Path(folder_or_job)
        jp = p if p.name == 'JOB.json' else p / 'JOB.json'
        job = json.loads(jp.read_text())
    else:
        job = folder_or_job
        jp = None
    pending = job.get('filament_warnings_pending') or {}
    warnings = list(warnings) if warnings is not None else list(pending.get('warnings') or [])
    if not warnings:
        raise ValueError('no filament warnings to accept; an acceptance is never recorded blank')
    if source_sha256 is None:
        source_sha256 = pending.get('source_sha256')
    if selected_plates is None:
        selected_plates = pending.get('selected_plates')
    job['filament_warning_acknowledgement'] = {
        'code': 'FILAMENT_WARNINGS_ACCEPTED',
        'acknowledged': True,
        'via': via,
        'warnings': warnings,
        'selected_plates': [str(p) for p in selected_plates or []],
        'source_sha256': source_sha256,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    job.pop('filament_warnings_pending', None)
    if is_path and jp is not None:
        write_job(jp.parent, job)
    return job


def _filament_ack_covers(job, warnings, source_sha256, selected_plates):
    """Whether the job's recorded acceptance covers exactly these warnings."""
    ack = job.get('filament_warning_acknowledgement') or {}
    if ack.get('acknowledged') is not True:
        return False
    if source_sha256 is not None and ack.get('source_sha256') != source_sha256:
        return False
    if {str(p) for p in ack.get('selected_plates') or []} != {str(p) for p in selected_plates or []}:
        return False
    return set(warnings) <= set(ack.get('warnings') or [])


def _enforce_filament_warnings(job, warnings, source_sha256, selected_plates, job_folder=None,
                               via_slice=False):
    """Present-or-proceed for one set of filament warnings.

    Returns the warnings when a bound acceptance already covers them (and
    records them on the job as the slice's filament_warnings provenance).
    Otherwise records them as the job's filament_warnings_pending — on disk
    when job_folder is given, so the surface that presents them and the
    operator's later acceptance resume from the same record — and raises
    FilamentWarningPending: without an acceptance the job does not proceed.

    A pass inside slice_job (via_slice) marks the acceptance used by that
    slicing run; the next slice_job consumes a used acceptance before its
    gates run, so a reslice presents the warnings again instead of silently
    reusing the acceptance. Passes at color-intake time do not mark it: the
    acceptance is given for the print attempt that then slices.
    """
    warnings = list(dict.fromkeys(warnings))
    if not warnings:
        return []
    if _filament_ack_covers(job, warnings, source_sha256, selected_plates):
        job['filament_warnings'] = warnings
        job.pop('filament_warnings_pending', None)
        if via_slice:
            job['filament_warning_acknowledgement']['used_by_slice'] = True
            if job_folder is not None:
                # Mark it used on disk now, not only with the artifact: a
                # slicing run that fails after this point must not leave the
                # acceptance reusable, so the next run presents the warnings
                # again (#1189: a reslice re-presents them).
                try:
                    on_disk = json.loads((Path(job_folder) / 'JOB.json').read_text())
                except (OSError, ValueError):
                    on_disk = None
                ack = on_disk.get('filament_warning_acknowledgement') if isinstance(on_disk, dict) else None
                if isinstance(ack, dict) and not ack.get('used_by_slice'):
                    ack['used_by_slice'] = True
                    write_job(job_folder, on_disk)
        return warnings
    job['filament_warnings_pending'] = {
        'warnings': warnings,
        'source_sha256': source_sha256,
        'selected_plates': [str(p) for p in selected_plates or []],
        'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    if job_folder is not None:
        write_job(job_folder, job)
    raise FilamentWarningPending(warnings, source_sha256=source_sha256,
                                 selected_plates=selected_plates)


def filament_fallback_slot(state, limit=None):
    """(slot, phrase): the slot a filament fallback prints from, and how it was chosen (#1189).

    The printer's active slot when the hardware record names one and it is
    loaded; otherwise the record's first (lowest-numbered) loaded slot; with
    no loaded-slot information at all, slot 1, and the phrase says so. The
    source is the hardware record print start launches from: the slot map
    configured_slots offers (filament_slots, or a single-extruder printer's
    one configured spool) and its active_slot. "Loaded" is a slot within
    the printer's slot count that the record maps to a filament material:
    a fallback never names a slot (or tool) the printer does not have.
    """
    try:
        limit = int(limit if limit is not None else position_limit())
    except (TypeError, ValueError):
        limit = FILAMENT_SLOTS
    loaded = []
    for key, rec in (configured_slots(state) or {}).items():
        try:
            slot = int(key)
        except (TypeError, ValueError):
            continue
        if (1 <= slot <= limit and isinstance(rec, dict)
                and isinstance(rec.get('material'), str) and rec['material'].strip()):
            loaded.append(slot)
    loaded.sort()
    active = (state or {}).get('active_slot')
    if type(active) is int and active in loaded:
        return active, f"slot {active}, the printer's active slot"
    if loaded:
        return loaded[0], f"slot {loaded[0]}, the printer's first loaded slot"
    return 1, 'slot 1, because the printer record has no loaded-slot information'


def _fallback_selection(state, slot):
    """The selection for a fallback slot, filled from the slot's record (validate_decisions resolves it)."""
    rec = (configured_slots(state) or {}).get(str(slot)) or {}
    return {'slot': slot, **{key: rec[key] for key in ('color', 'material', 'spoolman_id')
                             if isinstance(rec, dict) and rec.get(key) is not None}}


def _choice_label(manifest, key):
    """A color-choice key as the operator knows it: the part name, or the source filament."""
    kind, _, rest = str(key).partition(':')
    if kind == 'filament':
        return f'source filament {rest}'
    if kind == 'part':
        part = next((p for p in manifest.get('parts') or [] if str(p.get('key')) == rest), None)
        if part and part.get('name'):
            return f"part {part['name']}"
    return str(key)


def filament_fallbacks(manifest, decisions, selected_plates, state, limit=None, choices_needed=False):
    """(decisions to slice with, warnings) under the #1189 filament fallbacks.

    Owner decision (catduck, 2026-11-10): no filament condition hard-blocks
    a print. Three conditions that refused until #1189 now print from a
    deterministic fallback, named in a warning the operator accepts first:

    - a slot number past the printer's slot count prints from the fallback
      slot (filament_fallback_slot: the active slot, else the first loaded
      slot, else slot 1);
    - a used filament with no slot chosen on a selected plate prints from
      the slot already chosen for that same source filament (the part's own
      filament: a sibling part or its filament row), else the slot chosen
      for the first part on that plate that has one (the base filament),
      else the fallback slot; the parts or filament row left unchosen get
      that slot;
    - color choices never made (choices_needed and no decisions) print
      every part single-color from the fallback slot, exactly like a
      recorded single-color decision.

    Nothing here is persisted: with no condition the decisions come back
    unchanged and the warnings empty. Otherwise the returned decisions carry
    a 'fallback' record (the original decisions, the filament keys added,
    the warnings); decisions_digest identifies them by the original, and a
    fallback passed back in is re-derived from its original, so the same
    inputs always give the same decisions and warning texts. Decisions
    bound to another source are a decision-integrity refusal and are never
    repaired here.
    """
    original = decisions
    if isinstance(decisions, dict) and isinstance(decisions.get('fallback'), dict):
        original = decisions['fallback'].get('original')
        choices_needed = choices_needed or original is None
    try:
        limit = int(limit if limit is not None else position_limit())
    except (TypeError, ValueError):
        limit = FILAMENT_SLOTS
    sha = manifest.get('source_sha256')
    slot, how = filament_fallback_slot(state, limit)
    warnings, added = [], []
    if not original:
        if not choices_needed:
            return original, []
        effective = {'source_sha256': sha, 'mode': 'single_color',
                     'selection': _fallback_selection(state, slot)}
        warnings.append('color choices were never made for this print; every part prints '
                        f'single-color from {how}')
    elif not isinstance(original, dict) or original.get('source_sha256') != sha:
        return original, []
    else:
        effective = json.loads(json.dumps(original))
        mode = effective.get('mode')

        def past(selection):
            number = selection.get('slot') if isinstance(selection, dict) else None
            return type(number) is int and not 1 <= number <= limit

        if mode == 'single_color' and past(effective.get('selection')):
            bad = effective['selection']['slot']
            effective['selection'] = _fallback_selection(state, slot)
            warnings.append(f'the one-color choice names slot {bad}, but the printer has only '
                            f'{limit} slots; it prints from {how} instead')
        elif mode == 'per_part' and isinstance(effective.get('choices'), dict):
            choices = effective['choices']
            over = {}
            for key in sorted(choices):
                if past(choices[key]):
                    over.setdefault(choices[key]['slot'], []).append(key)
                    choices[key] = _fallback_selection(state, slot)
            for bad in sorted(over):
                names = ', '.join(_choice_label(manifest, key) for key in over[bad])
                verb = 'it prints' if len(over[bad]) == 1 else 'they print'
                warnings.append(f'slot {bad} is chosen for {names}, but the printer has only '
                                f'{limit} slots; {verb} from {how} instead')
            if selected_plates:
                from .source_colors import unmapped_plate_filaments
                for plate_id, ext, plate_parts in unmapped_plate_filaments(manifest, effective,
                                                                           selected_plates):
                    def chosen(key):
                        row = choices.get(key)
                        return row if isinstance(row, dict) and row.get('slot') is not None else None
                    holders = [p for p in plate_parts if p.get('source_extruder') == ext]
                    targets = [f"part:{p.get('key')}" for p in holders
                               if chosen(f"part:{p.get('key')}") is None] or [f'filament:{ext}']
                    if all(chosen(key) is not None for key in targets):
                        continue  # filled for an earlier plate: one slot per source filament
                    # The part's own filament: a slot already chosen for this
                    # source filament (a sibling part, or its filament row).
                    own = next((chosen(f"part:{p.get('key')}") for p in holders
                                if chosen(f"part:{p.get('key')}") is not None),
                               chosen(f'filament:{ext}'))
                    # Else the base filament: the first part on the plate with a slot.
                    base = next((p for p in plate_parts
                                 if chosen(f"part:{p.get('key')}") is not None), None)
                    if own is not None:
                        picked = dict(own)
                        named = (f"slot {picked['slot']}, the slot already chosen for source "
                                 f"filament {ext}")
                    elif base is not None:
                        picked = dict(chosen(f"part:{base.get('key')}"))
                        named = (f"slot {picked['slot']}, the slot chosen for part "
                                 f"{base.get('name') or base.get('key')}")
                    else:
                        picked, named = _fallback_selection(state, slot), how
                    for key in targets:
                        choices[key] = dict(picked)
                        added.append(key)
                    names = ', '.join(_choice_label(manifest, key) for key in targets
                                      if key.startswith('part:'))
                    warnings.append(f'plate {plate_id}: source filament {ext} is used but has no '
                                    f'slot chosen{" (" + names + ")" if names else ""}; '
                                    f'it prints from {named}')
    if not warnings:
        return original, []
    effective['fallback'] = {'original': original, 'added_keys': added, 'warnings': warnings}
    return effective, warnings




def _enforce_filament_readiness(job, manifest, decisions, selected_plates,
                                slot_capacity=None, job_folder=None, via_slice=False, state=None):
    """The selected-plate readiness gate under the filament warn-and-accept ruling.

    Readiness problems that are not filament data refuse exactly as
    require_plate_readiness always has (named, in one message). Filament
    problems — unknown usage, slicer/analysis disagreement, damaged usage
    claims — become warnings handled by _enforce_filament_warnings.

    With the printer record (`state`), the #1189 fallbacks apply first: a
    used filament with no slot chosen and a slot past the printer's slot
    count are filled by filament_fallbacks and become warnings naming the
    slot they print from, so they no longer refuse. Every caller that
    slices or saves decisions passes it; without it those rows refuse as
    before.
    """
    from .source_colors import (plate_readiness_problems, filament_readiness_warnings,
                                validate_plate_usage_if_present)
    fallback_warnings = []
    if state is not None:
        decisions, fallback_warnings = filament_fallbacks(manifest, decisions, selected_plates,
                                                          state, slot_capacity)
    problems = plate_readiness_problems(manifest, decisions, selected_plates,
                                        slot_capacity=slot_capacity)
    readiness_warnings = filament_readiness_warnings(manifest, decisions, selected_plates,
                                                     slot_capacity=slot_capacity)
    hard = [p for p in problems if p not in set(readiness_warnings)]
    if hard:
        raise RuntimeError('print readiness blocked: ' + '; '.join(problems))
    warnings = (fallback_warnings + readiness_warnings
                + validate_plate_usage_if_present(manifest, selected_plates))
    return _enforce_filament_warnings(job, warnings, manifest.get('source_sha256'),
                                      selected_plates, job_folder=job_folder, via_slice=via_slice)


def _whole_source_usage_warnings(manifest):
    """Filament warnings for every plate of a whole-source multi-plate job.

    No production surface writes job['selected_plates'], so a multi-plate
    source is sliced whole and the selected-plate readiness gate never runs
    for it (#1155). Its usage findings over all plates (an unknown usage
    class, a slicer/analysis disagreement, slicer usage that could not be
    validated) are still presented: they warn and the operator may accept
    them (owner ruling, catduck, 2026-11-10; #1189). Only the warning rows
    are taken. The mapping rows (a used filament with no validated slot)
    are never applied to a whole-source job: its color questions were not
    scoped per plate, so they would refuse jobs that slice correctly today.
    """
    from .source_colors import filament_readiness_warnings
    usage = manifest.get('plate_usage') or {}
    if not isinstance(usage, dict) or not usage:
        return []
    return filament_readiness_warnings(manifest, {}, sorted(usage, key=str))


def intake_color_choices(job_path, state, mode=None, slots=None, interactive=False):
    """Normal intake gate: ask per part/palette and persist exact-source choices.

    Noninteractive callers receive a structured button-ready question packet.
    They resume the same JOB via slice; no JSON file authoring is required.
    """
    from .source_colors import inventory, prompts, canonical_prompts, validate_decisions, distinct_source_colours
    path = Path(job_path)
    job = json.loads(path.read_text())
    manifest = inventory(job['downloaded_file'])
    # A job that records a plate selection asks only about the selected
    # plates' colors and validates decisions against that scope (#1110 B).
    selected_plates = job.get('selected_plates') or None
    if not manifest.get('source_sha256'):
        manifest['source_sha256'] = file_sha256(job['downloaded_file'])
    adapter = _job_adapter(job)
    multi_tool = adapter.tool_based
    # The slot questions below are built from the record's slot map, so they serve the
    # record-bound multi_slot printers (and, tool-shaped, the multi_tool pair). A Bambu
    # AMS model is multi_slot by class (#671) but not record-bound: its filament slots
    # are the file's own, matched to reported AMS trays at print start, so a source that
    # needs slot choices is refused here exactly as it was while the model was "single".
    if not multi_tool and not (adapter.multi_slot and adapter.slots_bound_by_record):
        if manifest['requires_choices']:
            # Height-based colors alone (#870) are not a slot choice: they
            # print as pauses for a manual swap or in one color, whichever
            # the recorded decisions say. Without a recorded choice the job
            # stops here and names that choice — never a silent one color.
            layered_only = (bool(manifest.get('layer_changes')) and len(manifest['parts']) <= 1
                            and not manifest['painted'])
            if layered_only and job.get('color_decisions'):
                validated = validate_decisions(manifest, job['color_decisions'], state,
                                               resolve_filament_selection,
                                               selected_plate_ids=selected_plates)
                if selected_plates:
                    _enforce_filament_readiness(job, manifest, validated, selected_plates,
                                                slot_capacity=position_limit(adapter),
                                                job_folder=path.parent, state=state)
                return True
            if layered_only:
                raise MultiColorUnsupported(
                    f"{adapter.label} has a single extruder; this source changes color by height. Choose "
                    "pauses at each change height (swap the filament by hand) or one color for everything "
                    "before slicing")
            raise MultiColorUnsupported(
                f"{adapter.label} has a single extruder; this source has several parts or colors that need a "
                "slot choice. Slice a single-color model or use a multi-slot printer")
        return True
    questions = (canonical_prompts(manifest, selected_plate_ids=selected_plates)
                 if job.get('color_schema') == 'part_slots_v1'
                 else prompts(manifest, selected_plate_ids=selected_plates))
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
        validated = validate_decisions(manifest, job['color_decisions'], state,
                                       resolve_filament_selection,
                                       selected_plate_ids=selected_plates)
        if selected_plates:
            _enforce_filament_readiness(job, manifest, validated, selected_plates,
                                        slot_capacity=position_limit(adapter),
                                        job_folder=path.parent, state=state)
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
    validated = validate_decisions(manifest, decisions, state, resolve_filament_selection,
                                   selected_plate_ids=selected_plates)
    if selected_plates:
        _enforce_filament_readiness(job, manifest, validated, selected_plates,
                                    slot_capacity=position_limit(adapter),
                                    job_folder=path.parent, state=state)
    set_color_decisions(job, validated)
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


def refuse_assembly_split(src, manifest, groups, st, verdict=None):
    """Refuse, before any plate is exported, a split that would cut one build object.

    The printable flag selects whole build objects, so an assembly (one
    object holding parts of several slots) cannot be divided across the
    per-slot plates; export_single_color_plate would only refuse later with
    an internal-looking error (#820). Raises a classified SliceRefusal that
    names the model's size against the bed and the way out. ``verdict`` is
    the planner's split verdict (job['plate_merge']); its reason decides
    whether the message talks about space, materials or the operator's
    own choice.
    """
    slots_by_object = {}
    for group in groups:
        keys = set(group['part_keys'])
        for part in manifest.get('parts') or []:
            if part['key'] in keys:
                slots_by_object.setdefault(str(part['object_id']), set()).add(group['slot'])
    shared = sorted(oid for oid, slots in slots_by_object.items() if len(slots) > 1)
    if not shared:
        return
    size = None
    try:
        from .color_geometry import _world_bounds
        with zipfile.ZipFile(src) as archive:
            boxes = _world_bounds(archive, set(shared))
        size = [max(b[axis][1] for b in boxes) - min(b[axis][0] for b in boxes) for axis in range(3)]
    except Exception:
        size = None  # the size is a courtesy in the message, never a reason to fail differently
    bed = (st.get('bed_mm') or [None, None])[:2]
    from .slice_refusal import assembly_cannot_split
    reason = (verdict or {}).get('reason')
    raise assembly_cannot_split(
        len({slot for oid in shared for slot in slots_by_object[oid]}), size,
        bed if all(bed) else None,
        f"build object(s) {','.join(shared)} hold parts of more than one slot and the plan was split "
        f"({reason or 'no reason recorded'}); an assembly cannot be split per slot. Nothing sliced",
        reason)


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
    out = {part['key']: view['choices']['part:' + part['key']]['slot'] for part in manifest['parts']}
    # Height-based color changes (#870): their filament choices are colors of
    # the plate too, so a plate whose parts all share one slot is not a
    # single-color plate when its layer changes switch to other slots.
    out.update({key: row['slot'] for key, row in view['choices'].items() if key.startswith('filament:')})
    return out


def record_support_decision(folder, supports, by='operator'):
    """Record the operator's supports choice on JOB.json (retries and reprints never ask again)."""
    path = Path(folder) / 'JOB.json'
    job = json.loads(path.read_text())
    job['support_decision'] = {'supports': bool(supports), 'by': by,
                               'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    write_job(folder, job)
    return job


def record_supports_question(folder, audit):
    """Record why the supports question was (or was not) shown (#1020 F3).

    ``audit`` carries the decision's reason and inputs plus the
    clearance report and the slicer finding the decision was made from,
    so a shown or hidden menu can be audited from JOB.json alone.
    """
    path = Path(folder) / 'JOB.json'
    job = json.loads(path.read_text())
    job['supports_question'] = {**audit, 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    write_job(folder, job)
    return job


def _support_kw(job, supports=None):
    """slice_model kwargs carrying a supports choice: the path's own, else the operator's.

    Every slicing path calls this so "Re-slice with supports" reaches Orca whichever
    branch (merged, painted, multi-tool, split, per-plate) slices the job.
    """
    decided = (job.get('support_decision') or {}).get('supports') if isinstance(job, dict) else None
    if decided is True:
        supports = True  # chosen after the slicer's warning: the latest operator word on supports
    elif supports is None and decided is False:
        supports = False
    if supports is None and isinstance(job, dict):
        # A chat edit's supports choice (#903) fills the remaining gap on
        # every path, early-return slicing paths included.
        from . import edit_plan as _edit_plan_sk
        supports = _edit_plan_sk.supports_override(job)
    return {'supports': supports} if supports is not None else {}


def _edit_kw(job):
    """slice_model kwargs carrying a job's chat edit settings (#903).

    Every slicing path passes these, so a layer height, infill or support
    type edit is never dropped on the merged, painted, multi-tool or
    per-plate paths (Codex P2 on #908). slice_model refuses a mirror on
    the per-part and preserve-layout paths, loudly, instead of dropping it.
    """
    from . import edit_plan as _edit_plan_ek
    edits = _edit_plan_ek.slice_edits(job) if isinstance(job, dict) else None
    return {'edits': edits} if edits else {}


def _job_fit(job, brim_override):
    """Operator-chosen scale/orientation for per-part plates.

    Returns (fit, supports, brim). fit is None at authored size. The same
    verified option the whole-model path honors is applied to every slot
    plate: rotation, uniform scale, and the supports/brim it was offered with.
    """
    scale = job.get('scale')
    orientation = job.get('orientation') or {}
    rotation = orientation.get('rotation')
    decided = (job.get('support_decision') or {}).get('supports')  # operator's supports choice
    decided = decided if isinstance(decided, bool) else None
    if not rotation and (scale is None or float(scale) == 1.0):
        if brim_override is None and job.get('fit_brim_policy'):
            brim_override = job['fit_brim_policy'].get('width_mm', 0)
        return None, decided, brim_override
    fit = {'scale': float(scale) if scale is not None else 1.0, 'rotation': rotation}
    if rotation:
        # A rotation that carries no supports value leaves the choice
        # unresolved (#916): bool(None) here forced supports off on every
        # per-plate slice and marked the question decided.
        _orient_supports = orientation.get('supports')
        supports = _orient_supports if isinstance(_orient_supports, bool) else decided
    else:
        supports = decided
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


def full_size_one_plate(source, state, decisions=None, capacity=None, multi_tool=False, job=None):
    """A 100%-scale, one-plate rearrangement the slicer will honor exactly, or None.

    This is the cheap first fit check after the author's layout fails the bed:
    no Orca probe runs. It answers with the planner the slice path uses, so an
    arrangement it reports is one slicing realizes:

    * per-part color decisions on a 3MF slice through the merge planner
      (``plate_merge.plan``) with the rearrangement flag, which keeps the
      merge spacing, bed-edge margin and prime-tower room for multi-slot jobs;
      ``capacity``/``multi_tool`` carry the job printer's position count and
      kind into that planner, exactly as ``slice_multi_plate`` passes them;
      ``job`` is the real job, carried into the planner under the flag: the
      planner reads the author's own text from it (a listing summary asking
      for separate printing), which a synthetic flag-only job hid (P2-235);
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
        flagged = {**(job or {}), 'full_size_rearrange_plan': {'rearrange_plate': True}}
        verdict = plate_merge.plan(source, manifest, _plates_for(source, manifest), slots, st,
                                   bed=int(bed[0]), job=flagged,
                                   edge=_merge_edge(st.get('brim_override')),
                                   capacity=capacity, multi_tool=multi_tool)
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
        adapter = _job_adapter(job)
        # The planner's capacity is the job printer's position count: a
        # multi_tool model's toolheads, not the configured printer's IFS
        # slots (a Creator-configured process has filament_slots=1) (P1-72).
        verdict = plate_merge.plan(src, manifest, plates, slots, st, job=job, edge=_merge_edge(plan_brim),
                                   capacity=position_limit(adapter), multi_tool=adapter.tool_based)
        if verdict['mode'] == 'merge':
            merged_slice = (_slice_merged_multi_tool if adapter.tool_based else _slice_merged)
            try:
                result = merged_slice(job, job_path, src, manifest, decisions, view, verdict, st, brim_override, work)
            except plate_merge.SplitNeeded as exc:
                verdict = {**verdict, 'mode': 'split', 'reason': str(exc)}
            else:
                return result
    job['plate_merge'] = {k: v for k, v in verdict.items()
                          if k not in ('shifts', 'placed_bounds_mm', 'yaw_by_object')}
    if _job_adapter(job).tool_based:
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


def painted_plate_scale(src, view, object_ids, ceiling=1.0, tower_mm=None):
    """Largest ladder scale <= ceiling at which this plate plus its prime tower
    fits the bed. Pure geometry measurement, no slicer, no printer.
    tower_mm is the tower width to reserve (0 for a printer that purges into its chute).

    Each step is measured, not written (#1246): the export's measure-only
    mode runs the same checks and gives the same part bounds and tower
    verdict as a written export, without building a 3MF nobody reads.
    """
    from . import painted_geometry
    from .color_geometry import export_color_geometry
    # The exact ceiling is tried first, whatever its size: the confirm
    # prompt's scaling records arbitrary cumulative scales (99%, 105%),
    # and printing the nearest ladder step instead would change the size
    # the operator approved. The ladder below is the fallback for a
    # ceiling that does not fit as-is; it only ever shrinks.
    steps = [ceiling] + [s for s in PAINTED_SCALE_STEPS if s < ceiling - 1e-9]
    with painted_geometry.decode_scope():
        for scale in steps:
            if scale > ceiling + 1e-9:
                continue
            try:
                _, geometry = export_color_geometry(src, None, view, object_ids=object_ids, scale=scale,
                                                    tower_mm=tower_mm)
            except RuntimeError:
                continue
            if all(0 <= ax[0] and ax[1] <= BED_LIMIT_MM for row in geometry['parts'] for ax in row['bounds_mm'][:2]):
                return scale
    return None


BED_LIMIT_MM = BED_MM


def _painted_tower_mm(state):
    """The tower width a painted-plate fit reserves for this printer: None keeps the configured one."""
    if not state:
        return None
    plan = purge.resolve(state=state)
    return 0.0 if plan.zmod and plan.towerless else None


def painted_plate_layout(src, view, object_ids, scale, tower_mm=None):
    """Measured layout of one painted plate at ``scale``, or None when unmeasurable.

    Display only, for fit-refusal wording (#839): the parts' extent and the
    prime tower footprint the fit reserves (tower_mm None when the printer
    needs no tower, #832). A failed measurement is not a fit verdict, so
    every failure returns None and the caller words the refusal without
    measured numbers.
    """
    from . import painted_geometry
    from .color_geometry import export_color_geometry
    try:
        # Measure towerless (#839): the part bounds do not depend on
        # tower placement, but the export raises when no tower position
        # clears the parts — exactly the no-fit case this measurement
        # exists to explain. The tower size is reported from tower_mm
        # below, not from a placement that may not exist. Measured, not
        # written (#1246).
        with painted_geometry.decode_scope():
            _, geometry = export_color_geometry(src, None, view,
                                                object_ids=object_ids, scale=scale, tower_mm=0.0)
    except Exception:
        return None
    bounds = geometry.get('build_bounds_mm') or []
    if len(bounds) < 2:
        return None
    try:
        extent = [round(float(bounds[a][1]) - float(bounds[a][0]), 1) for a in range(2)]
    except (TypeError, ValueError, IndexError):
        return None
    tower = None
    if tower_mm is None:
        from .color_geometry import effective_tower_mm
        tower = float(effective_tower_mm())
    elif float(tower_mm) > 0:
        tower = float(tower_mm)
    return {'extent_mm': extent, 'tower_mm': tower, 'scale': float(scale)}


def painted_fit(src, view, plates, ceiling=1.0, tower_mm=None):
    """One shared scale for every authored plate of a painted source.

    The whole fit runs in one decode scope and one inventory scope, so the
    source is read, inventoried and parsed once for every plate and step.
    """
    from . import painted_geometry
    from .source_colors import inventory_scope
    with painted_geometry.decode_scope(), inventory_scope():
        return _painted_fit(src, view, plates, ceiling, tower_mm)


def _painted_fit(src, view, plates, ceiling, tower_mm):
    scale, blocking = ceiling, None
    for plate in plates:
        ids = {str(o) for o in plate['object_ids']}
        fitted = painted_plate_scale(src, view, ids, scale, tower_mm)
        if fitted is None:
            result = {'scale': None, 'blocking_plate': plate['id'], 'plates': len(plates)}
            layout = painted_plate_layout(src, view, ids, PAINTED_SCALE_STEPS[-1], tower_mm)
            if layout is not None:
                result['blocking_layout'] = layout
            return result
        if fitted < scale:
            scale, blocking = fitted, plate['id']
    return {'scale': scale, 'blocking_plate': blocking, 'plates': len(plates)}


def painted_fit_for_job(src, decisions, state=None):
    """Intake helper: shared painted-plate scale for an exact-source decision.

    The whole fit runs in one inventory scope (#1153): every geometry export
    it tries (up to one per ladder step per plate) and the plate manifest
    ask for the source's color manifest, and inside the scope a source whose
    bytes hash the same is inventoried once. Each caller still gets its own
    copy, and nothing outlives the call.
    """
    from .source_colors import inventory_scope
    with inventory_scope():
        return _painted_fit_for_job(src, decisions, state)


def _painted_fit_for_job(src, decisions, state=None):
    from .source_colors import inventory
    manifest = inventory(src)
    view = _per_part_view(manifest, decisions)
    try:
        plates = plate_manifest(src)['plates']
    except RuntimeError:
        plates = [{'id': 1, 'object_ids': sorted({str(p['object_id']) for p in manifest['parts']})}]
    plates = [p for p in plates if any(str(o) in {str(x['object_id']) for x in manifest['parts']} for o in p['object_ids'])]
    return painted_fit(src, view, plates, tower_mm=_painted_tower_mm(state))


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
    fit = painted_fit(src, view, plates, float(job.get('scale') or 1.0), tower_mm=_painted_tower_mm(st))
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
                                               object_ids=ids, scale=None if scale == 1.0 else scale,
                                               **_support_kw(job), **_edit_kw(job))
        geometry = provenance.get('geometry') or {}
        if sorted(r['part_key'] for r in geometry.get('parts', [])) != sorted(keys):
            raise RuntimeError(f"painted plate {plate['id']} dropped or duplicated a source part")
        check = verify_gcode_offline(out, st, _job_adapter(job))
        tools = provenance.get('tool_mapping') or {}
        artifacts.append({'plate_id': plate['id'], 'slot': sorted(set(tools.values())),
                          'binding_key': f"{plate['id']}:painted", 'part_keys': keys,
                          'scale': scale, 'gcode': str(out), 'offline_check': check,
                          'estimate_s': gcode_estimate_s(Path(out).read_text(errors='ignore')),
                          'provenance': provenance, 'slice_config': [str(p) for p in configs]})
    if not artifacts:
        raise RuntimeError('painted source produced no plates')
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
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
        out, provenance, configs = slice_model(plate, slice_dir, st, brim_override, preserve_layout=True, filament_slot=slots[0],
                                               **_support_kw(job), **_edit_kw(job))
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
                                               item_shifts=verdict['shifts'], **_support_kw(job), **_edit_kw(job),
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
            placed_by_object = _placed_bounds_by_object(geometry, object_of_key)
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
    check = verify_gcode_offline(out, st, _job_adapter(job))
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
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
                                           item_shifts=verdict['shifts'], **_support_kw(job), **_edit_kw(job),
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
        placed_by_object = _placed_bounds_by_object(geometry, object_of_key)
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
    check = verify_gcode_offline(out, st, _job_adapter(job))
    latest = json.loads(Path(job_path).read_text())
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
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
            out, provenance, configs = slice_model(plate, slice_dir, st, brim_override, preserve_layout=True, filament_slot=slot, **_support_kw(job, supports), **_edit_kw(job))
            finalize_multicolor(out, None, st, {'0': slot})
            check = verify_gcode_offline(out, st, _job_adapter(job))
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
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
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
                                               filament_slot=slot, **_support_kw(job, supports), **_edit_kw(job))
        finalize_multicolor(out, None, st, {'0': slot})
        check = verify_gcode_offline(out, st, _job_adapter(job))
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
    if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
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
    job = json.loads(Path(job_path).read_text())
    # A filament warning acceptance authorizes one slicing run: an
    # acceptance an earlier slice_job run already used is consumed here,
    # before any gate reads it, so a reslice presents the warnings again
    # instead of silently reusing the acceptance (the clearance pattern's
    # rule that a reslice invalidates the pending acknowledgement).
    _filament_ack = job.get('filament_warning_acknowledgement') or {}
    if _filament_ack.get('used_by_slice'):
        job.pop('filament_warning_acknowledgement', None)
        job.pop('filament_warnings', None)
        write_job(Path(job_path).parent, job)
    # The job's own printer record (#334): slicing a secondary printer's job
    # against the default record would validate its filament selection
    # against the wrong printer's slots and slice for the wrong hardware
    # (SAFE-4 / P1-78). A job with no recorded printer keeps the legacy load.
    st = printer_state.load(job["printer_id"]) if job.get("printer_id") else printer_state.load()
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
    _recorded_slot = (job.get("filament_selection") or {}).get("slot") if isinstance(job.get("filament_selection"), dict) else None
    _slot_past_limit = type(_recorded_slot) is int and not 1 <= _recorded_slot <= position_limit(adapter)
    if not generic and job.get("filament_selection") is not None and (job.get('color_decisions') or {}).get('mode') != 'per_part' \
            and not (_slot_past_limit and job.get('color_decisions')):
        # A recorded one-color slot past the printer's slot count is not
        # refused here: the #1189 fallback below replaces it (named in a
        # warning the operator accepts) and validates the replacement.
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
    if src.suffix.lower() == ".3mf":
        with zipfile.ZipFile(src) as archive:
            actual_plates = {int(match.group(1)) for name in archive.namelist()
                             if (match := re.fullmatch(r"Metadata/plate_(\d+)\.(?:png|gcode)", name))}
            if "Metadata/model_settings.config" in archive.namelist():
                try:
                    settings = ET.fromstring(archive.read("Metadata/model_settings.config"))
                    # Every plate with a plater_id counts, with or without a <model_instance>:
                    # an exporter that omits instances (and thumbnails) still names separate
                    # beds, which must never be merged onto one (the same rule as audit()).
                    for plate in settings.iter("plate"):
                        for metadata in plate.iter("metadata"):
                            if metadata.get("key") == "plater_id":
                                plate_id = metadata.get("value")
                                if plate_id is None:
                                    raise RuntimeError("3MF plate assignment has no plater_id value")
                                actual_plates.add(int(plate_id))
                except (ET.ParseError, ValueError, TypeError) as exc:
                    raise RuntimeError(f"unable to inspect 3MF plate assignments: {exc}") from exc
        multi_plate = len(actual_plates) > 1 or int(ad.get("plates", 0) or 0) > 1

    from .source_colors import inventory, prompts, require_single_color, validate_decisions
    try:
        manifest = inventory(src)
    except RuntimeError as exc:
        # Preserve the legacy safety contract for malformed, thumbnail-less
        # multi-plate archives before surfacing component-reference errors.
        has_thumbnails = False
        if src.suffix.lower() == ".3mf":
            with zipfile.ZipFile(src) as archive:
                names = archive.namelist()
                try:
                    settings = ET.fromstring(archive.read("Metadata/model_settings.config"))
                    plate_ids = {m.get("value") for m in settings.iter("metadata")
                                 if m.get("key") == "plater_id"}
                except (KeyError, ET.ParseError):
                    plate_ids = set()
                has_thumbnails = any(re.fullmatch(r"Metadata/plate_\d+\.png", name) for name in names)
            if len(plate_ids) > 1 and not has_thumbnails:
                raise RuntimeError("multi-plate 3MF source has no plate thumbnails; no slicing attempted") from exc
        # Owner ruling (catduck, 2026-11-10): unknown filament usage never
        # blocks a print; the job proceeds to the slicer. A plates-only
        # source — the archive carries its plates as thumbnails, the job's
        # audit accounts for every one of them, and the audit records no
        # build parts — has nothing for inventory to resolve: parts and
        # usage stay UNKNOWN and the slicer's output is authoritative.
        # The refusal below stands for the rest: a stale audit that hides
        # plates, and build parts the inventory cannot assign to plates,
        # are safety facts about the job record, not usage gaps.
        plates_only_unknown = (
            multi_plate and has_thumbnails
            and int(ad.get("plates", 0) or 0) == len(actual_plates)
            and not int(ad.get("build_items", 0) or 0))
        if plates_only_unknown:
            manifest = {'source_sha256': file_sha256(src), 'parts': [], 'filaments': [],
                        'painted': False, 'requires_choices': False,
                        'layer_changes': [], 'layer_extras': [], 'plate_usage': {}}
            from .source_colors import _append_filament_stats
            _append_filament_stats(src, manifest)
        elif multi_plate:
            raise RuntimeError(f'multi-plate 3MF cannot be merged or split: {exc}; no slicing attempted') from exc
        else:
            raise
    selected_plates = job.get('selected_plates') or None
    filament_gate_ran = False
    # The #1189 fallbacks (owner decision, catduck, 2026-11-10: no filament
    # condition hard-blocks a print). On a printer whose decisions are
    # sliced (multi-slot, or multi_tool), a slot past the printer's slot
    # count, a used filament with no slot chosen and color choices never
    # made print from a deterministic fallback slot instead of refusing.
    # The decisions are derived in memory from JOB.json's color_decisions
    # and the printer record, before any slicing, so the G-code is made for
    # slots the printer has and passes the same bounds and tool checks.
    # Their warnings join the filament warning round below: nothing slices
    # until the operator accepts them, and a reslice presents them again.
    fallback_used = not generic or multi_tool
    slice_decisions_in, fallback_warnings = job.get('color_decisions'), []
    if fallback_used:
        if not manifest.get('source_sha256'):
            manifest['source_sha256'] = file_sha256(src)
        slice_decisions_in, fallback_warnings = filament_fallbacks(
            manifest, job.get('color_decisions'), selected_plates, st, position_limit(adapter),
            choices_needed=bool(manifest['requires_choices']))
    if multi_plate and selected_plates and slice_decisions_in:
        # The user selected exact source plates, so validate their usage and
        # mappings before slicing. Whole-source imports without source usage
        # metadata remain unknown until Orca provides authoritative output.
        _enforce_filament_readiness(job, manifest, slice_decisions_in, selected_plates,
                                    slot_capacity=position_limit(adapter),
                                    job_folder=Path(job_path).parent, via_slice=True,
                                    state=st if fallback_used else None)
        filament_gate_ran = True
    usage_warnings = []
    if multi_plate:
        # Source usage is evidence, not a prerequisite for slicing an unsliced
        # project. Damaged claims when present become filament warnings;
        # absent metadata stays UNKNOWN and the destination slicer's output
        # becomes authoritative. A job with a plate selection is warned only
        # about the selected plates' claims: an unselected plate never
        # contributes (#1110 Phase B).
        from .source_colors import validate_plate_usage_if_present
        usage_warnings = validate_plate_usage_if_present(manifest, selected_plates)
        if not selected_plates:
            usage_warnings = _whole_source_usage_warnings(manifest) + usage_warnings
    # A height-color-only source (#870) is not a multi-color source: its parts
    # share one extruder and the recorded single-color decision carries the
    # operator's pause-or-one-color choice, which a single-extruder printer
    # honours through slice_model's pause path. It is exempt from the generic
    # refusal and the decisions drop below — but only that decision shape,
    # and never for multi-plate sources or full-size split plans.
    recorded = job.get('color_decisions')
    layered_single = bool(
        not multi_plate and not job.get('full_size_split_plan')
        and not manifest.get('painted') and manifest.get('layer_changes')
        and len({part['source_extruder'] for part in manifest['parts']}) <= 1
        and isinstance(recorded, dict) and recorded.get('mode') == 'single_color')
    if generic and (multi_plate or manifest['requires_choices'] or job.get('full_size_split_plan')):
        # A multi_tool printer honours multi-colour and multi-plate sources
        # through the identity tool mapping (#346); a full-size split plan is
        # still IFS-shaped and stays refused here.
        if (not multi_tool or job.get('full_size_split_plan')) and not layered_single:
            raise MultiColorUnsupported(
                f"{adapter.label} has a single extruder; multi-plate, multi-part or multi-color sources need a "
                "multi-slot printer. Slice one single-color plate instead")
    # STL has no source palette; bind decisions to the exact file bytes, as intake does.
    if not manifest.get('source_sha256'):
        manifest['source_sha256'] = file_sha256(src)
    job['color_manifest'], job['color_prompts'] = manifest, prompts(manifest, selected_plate_ids=selected_plates)
    write_job(Path(job_path).parent, job)
    _timing(job_path, 'inventory_done')
    job['slice_recipe'] = recipe  # persisted only with the artifact it describes
    if fallback_warnings and not filament_gate_ran:
        # Present the fallbacks before the decisions are validated: the
        # operator sees the named slot first, and a fallback slot that the
        # record cannot resolve (no loaded-slot information) refuses only
        # after acceptance, as the slot check it is.
        _enforce_filament_warnings(job, fallback_warnings + usage_warnings,
                                   manifest.get('source_sha256'), selected_plates,
                                   job_folder=Path(job_path).parent, via_slice=True)
        filament_gate_ran = True
    decisions = None if (generic and not multi_tool and not layered_single) else slice_decisions_in
    if decisions is not None:
        decisions = validate_decisions(manifest, decisions, st, resolve_filament_selection,
                                       selected_plate_ids=selected_plates)
        if selected_plates:
            # Automated readiness for a selected-plate print: a selected
            # plate whose used colors cannot be validated against the
            # decisions blocks here, before any slicing; unselected plates
            # contribute nothing (#1110 Phase B, criterion 6). Filament
            # usage findings warn instead of blocking (owner ruling,
            # catduck, 2026-11-10) — see _enforce_filament_readiness.
            _enforce_filament_readiness(job, manifest, decisions, selected_plates,
                                        slot_capacity=position_limit(adapter),
                                        job_folder=Path(job_path).parent, via_slice=True,
                                        state=st if fallback_used else None)
            filament_gate_ran = True
    if (usage_warnings or fallback_warnings) and not filament_gate_ran:
        # No readiness gate presented these (no plate selection, or no
        # decisions for it to check): damaged usage claims and the #1189
        # fallbacks still wait for the operator's acceptance before slicing.
        _enforce_filament_warnings(job, fallback_warnings + usage_warnings,
                                   manifest.get('source_sha256'), selected_plates,
                                   job_folder=Path(job_path).parent, via_slice=True)
    if fallback_warnings:
        # Accepted: record what this artifact is sliced with (provenance;
        # JOB.json's color_decisions stay the operator's own). A one-color
        # fallback is the job's slot selection, exactly as a recorded
        # one-color decision is, and print start launches from it.
        job['filament_fallback'] = {'warnings': fallback_warnings, 'decisions': decisions,
                                    'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
        fallback_selection = (decisions or {}).get('selection') \
            if (decisions or {}).get('mode') == 'single_color' else None
        if isinstance(fallback_selection, dict) and job.get('filament_selection') != fallback_selection:
            if not generic and not multi_tool:
                _validate_slot_selection(st, fallback_selection)
            job['filament_selection'] = fallback_selection
            on_disk = json.loads(Path(job_path).read_text())
            on_disk['filament_selection'] = fallback_selection
            write_job(Path(job_path).parent, on_disk)
            # The recipe hashes the selected slot: record it for the slot this artifact is sliced for.
            recipe = sr.record(sr.slice_recipe(job, file_sha256(src), st, copies=copies, scale=scale,
                                               brim_override=brim_override))
            job['slice_recipe'] = recipe
    else:
        job.pop('filament_fallback', None)
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
        job['filament_selection'] = selection
        if multi_tool and src.suffix.lower() == '.3mf':
            # A toolhead pick is a one-tool identity job: route it through
            # the merged multi-tool slice (#346 machinery) so the geometry
            # and filament profiles land on the picked toolhead instead of
            # T0. The generic tail below has no tool channel to honor it,
            # and it recorded the pick while silently slicing toolhead 1.
            # The merged path slices the authored geometry once, so a job
            # asking for copies, a resize or a rotation refuses here with
            # the reason stated instead of silently dropping the transform.
            scale_now = scale if scale is not None else job.get('scale')
            if (copies and int(copies) > 1) or (job.get('orientation') or {}).get('rotation') \
                    or (scale_now is not None and float(scale_now) != 1.0):
                raise RuntimeError(
                    f"{adapter.label} slices a single-color toolhead job as one merged plate at "
                    "authored size; copies, resizing and rotation are not supported on that path. "
                    "Slice one copy at 100% to print on the picked toolhead. Nothing sliced")
            try:
                return slice_multi_plate(job, job_path, src, manifest, decisions, st, brim_override)
            except ImplicitPlateSplit as exc:
                raise RuntimeError(
                    f"{adapter.label} prints a single-color toolhead job as one merged plate; this source "
                    f"does not merge onto one bed ({exc.verdict.get('reason')}). No slicing attempted") from exc
        if not multi_tool:
            # The IFS slot verification is slot-shaped; a toolhead pick was
            # already resolved against the recorded toolheads (#346).
            _validate_slot_selection(st, selection)
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
        refuse_assembly_split(src, manifest, groups, st, job.get('plate_merge'))
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
            # Chat edit settings (#903) ride onto every derived plate too:
            # the supports choice the edit made outranks the fit flow's
            # (it becomes the value _support_kw starts from; the operator's
            # support decision still applies on top, per _support_kw), and
            # the process edits apply per plate (a mirror in them is
            # refused by slice_model's per-part guard, loudly).
            from . import edit_plan as _edit_plan_pp
            _pp_supports = _edit_plan_pp.supports_override(job)
            _effective_supports = _pp_supports if _pp_supports is not None else supports
            out, provenance, configs = slice_model(plate, slice_dir, st, brim_override, preserve_layout=True, filament_slot=group['slot'], **_support_kw(job, _effective_supports), **_edit_kw(job))
            # Keep the plate's physical IFS slot explicit. Orca may retain
            # project-wide unused filament_type entries although all geometry
            # is remapped to logical T0 on this single-color derivative.
            material = st.get('filament_slots', {}).get(str(group['slot']), {}).get('material')
            if not material:
                raise RuntimeError(f"derived plate physical IFS slot {group['slot']} has no verified material")
            finalize_multicolor(out, None, st, {'0': group['slot']})
            # Verify the exact final bytes (after map, color and MD5 rewrite).
            check = verify_gcode_offline(out, st, _job_adapter(job))
            artifacts.append({'slot': group['slot'], 'part_keys': group['part_keys'], 'plate': str(plate),
                              'gcode': str(out), 'offline_check': check, 'plate_report': report,
                              'provenance': provenance, 'slice_config': [str(path) for path in configs]})
        latest = json.loads(Path(job_path).read_text())
        if file_sha256(src) != source_sha or decisions_digest(latest.get('color_decisions')) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
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
    slice_decisions = decisions
    if decisions is not None and slice_src != src:
        # The decisions were validated against the original source above, but
        # the slicer validates them against the file it is handed — the
        # arranged derivative, whose bytes (and SHA) differ. Bind the copy
        # passed to the slicer to the derivative; JOB.json and decision_sha
        # keep the original decisions (P1-50).
        slice_decisions = {**decisions, 'source_sha256': file_sha256(slice_src)}
    # A single-slot printer slices as `generic`, which leaves `decisions` as None, but the Discord
    # intake has still written color_decisions into JOB.json. The post-slice check below hashes
    # the file's decisions, so hash the same thing here or every such slice is refused (#268).
    decision_sha = decisions_digest(decisions if not generic else job.get("color_decisions"))
    selected_slot = None if generic else (job.get("filament_selection") or {}).get("slot")
    scale_kw = {'scale': scale} if scale is not None else {}
    if adapter.key != printers.current().key:
        scale_kw['adapter'] = adapter
    if rotation:
        scale_kw.update(rotation=rotation)
        # Only a supports value the orientation actually carries is a
        # choice (#916): a rotate/lay-flat edit adds a rotation without
        # one, and bool() turned that absence into supports=False plus a
        # support_decided=True provenance, suppressing the support
        # question. _support_kw preserves an existing orientation choice,
        # the operator's support decision and a chat edit's choice, and
        # leaves the choice unresolved when none of them exists.
        _orient_supports = orientation.get("supports")
        scale_kw.update(_support_kw(job, _orient_supports
                                    if isinstance(_orient_supports, bool) else None))
    else:
        scale_kw.update(_support_kw(job))  # the operator's choice, not the author's enable_support
    # Chat edit plans (#903): a standalone supports choice has no other
    # channel (the fit flow's orientation supports only rides along with a
    # rotation), and the process-level edit settings ride as `edits`. The
    # edit's supports fills only a gap: a supports value already decided
    # above (rotation, or the operator's support decision) stands.
    from . import edit_plan as _edit_plan
    _job_edits = _edit_plan.slice_edits(job)
    if _job_edits:
        scale_kw['edits'] = _job_edits
    _supports_edit = _edit_plan.supports_override(job)
    if _supports_edit is not None and 'supports' not in scale_kw:
        scale_kw['supports'] = _supports_edit
    _timing(job_path, 'orca_begin')
    if selected_slot is not None:
        # Single-color jobs are bound to their named IFS slot exactly like
        # derived per-slot plates, so launch uses the mapped native route.
        out, provenance, configs = slice_model(slice_src, slice_dir, st, brim_override,
                                              color_decisions=slice_decisions, filament_slot=selected_slot,
                                              copies=copies, **scale_kw)
        finalize_multicolor(out, None, st, {"0": selected_slot})
    else:
        out, provenance, configs = slice_model(slice_src, slice_dir, st, brim_override,
                                              color_decisions=slice_decisions, copies=copies, **scale_kw)
    # #952: the interval above mixes the slicer subprocess with Print Handy's
    # own finalize/guard work; the split rides along as extras (and in the
    # slice provenance) so a "speedup not visible" report is answerable from
    # TIMING.jsonl alone.
    _split = (provenance or {}).get('timing_s') or {}
    _timing(job_path, 'orca_finalize_done',
            **{k: _split[k] for k in ('slicer_s', 'python_s') if k in _split})
    _timing(job_path, 'verify_begin')
    if adapter.key == printers.current().key:
        _verify_routed(out, adapter, st, _recorded_printer_id(job), job=job)
    latest = json.loads(Path(job_path).read_text())
    _timing(job_path, 'verify_done')
    if file_sha256(src) != source_sha or decisions_digest(latest.get("color_decisions")) != decision_sha or _recipe_changed(job, latest, _recorded_printer_id(job)):
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


def _selected_plate_stats(manifest, selected):
    """A manifest copy whose filament totals cover only the selected plates.

    Uses the slicer-reported per-plate records; returns None when any
    selected plate lacks a validated record, so the caller keeps the
    whole-source totals rather than presenting a partial sum as the total.
    """
    stats = (manifest or {}).get('filament_stats') or {}
    plates = stats.get('plates') if isinstance(stats, dict) else None
    if not isinstance(plates, dict):
        return None
    extruders, total_g, total_m = {}, 0.0, 0.0
    for plate_id in selected:
        row = plates.get(str(plate_id))
        if not isinstance(row, dict) or row.get('usage_known') is not True:
            return None
        records = row.get('extruders') or {}
        if not isinstance(records, dict):
            return None
        for ext, amounts in records.items():
            # A record missing an amount, or carrying a non-number, is not
            # a validated record (#1155): keep the whole-source totals
            # rather than fail the projection with a KeyError.
            try:
                ext_id, grams, meters = int(ext), float(amounts['g']), float(amounts['m'])
            except (KeyError, TypeError, ValueError):
                return None
            entry = extruders.setdefault(ext_id, {'g': 0.0, 'm': 0.0})
            entry['g'] += grams
            entry['m'] += meters
            total_g += grams
            total_m += meters
    scoped = {**stats, 'extruders': extruders, 'total_g': round(total_g, 1),
              'total_m': round(total_m, 1)}
    scoped.pop('estimated', None)
    return {**manifest, 'filament_stats': scoped}


def filament_usage_by_selection(job, manifest=None):
    """Aggregate source estimates by confirmed selection, without duplicating multi-color totals."""
    manifest = manifest or (job or {}).get('color_manifest') or {}
    selected = (job or {}).get('selected_plates')
    if selected:
        # A selected-plate print spends only the selected plates' filament:
        # scope the totals to those plates' validated slicer records (#1110
        # Phase B). The reference-manifest flattening must not run on the
        # scoped totals (unselected plates' extruders are absent by design).
        scoped = _selected_plate_stats(manifest, selected)
        if scoped is not None:
            manifest = scoped
        else:
            manifest, _ = _reference_manifest(job, manifest)
    else:
        manifest, _ = _reference_manifest(job, manifest)
    # A #1189 fallback artifact accounts for the slots it was sliced with.
    decisions = effective_color_decisions(job) or {}
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


class IntakeSpoolWeights(dict):
    """Spool weights plus whether the live Spoolman fetch failed (#839).

    A plain dict for every existing reader; ``fetch_failed`` carries the
    availability signal alongside the fallback weights, so a caller can
    tell amounts that came from a live check from amounts that came from
    the cached slot record after Spoolman did not answer.
    """

    fetch_failed = False


def intake_spool_weights(state, spool_ids, fetch=None):
    """Currently available grams per Spoolman ID: Spoolman first, then the slot record, else absent.

    Display only. Never raises; an unreachable Spoolman just falls back,
    and the returned weights carry ``fetch_failed`` so the fallback is
    never mistaken for a live check (#839).
    """
    slots = {s.get('spoolman_id'): s for s in ((state or {}).get('filament_slots') or {}).values()
             if isinstance(s, dict) and s.get('spoolman_id') is not None}
    weights = IntakeSpoolWeights()
    for sid in spool_ids:
        grams = None
        if fetch is not None and isinstance(sid, int) and not isinstance(sid, bool):
            try:
                rem = (fetch(sid) or {}).get('remaining_weight')
                if isinstance(rem, (int, float)) and not isinstance(rem, bool) and rem >= 0:
                    grams = float(rem)
            except Exception:
                grams = None
                weights.fetch_failed = True
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




# The Moonraker start truth (#1318): the baseline classifier and its helpers live in start_truth.
StartReadback = start_truth.StartReadback
classify_start_readback = start_truth.classify_start_readback
_same_job = start_truth._same_job
















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














def _native_file_matches(reported, remote):
    """Match only documented Klipper paths for this upload, including ZMod's 3MF unpack path."""
    return launch_state.same_remote(reported, remote)






def _reconcile_native_submission(st, remote):
    """Read status once after lost POST response; never attribute heaters alone to this job."""
    return _read_start_readback(st, remote)[1]


































# Normalized states that mean "accepted, still starting" (heating the bed, running its calibration).
START_TRANSITIONAL_STATES = start_truth.START_TRANSITIONAL_STATES






def _reports_started(live, remote):
    """Printing or a transitional state, naming this upload (launch_state.same_remote): start_truth.polled."""
    return start_truth.polled(live, remote).confirmed








































def _bambu_reports_file(live, remote):
    """True when a normalized Bambu status shows `remote` running or preparing.

    `printing` covers gcode_state RUNNING, PREPARE and SLICING: an A1 unpacking a package sits
    in PREPARE before RUNNING, and both prove the start reached it. The file name is the
    `subtask_name` the start command set, so exact; the bare project name (the printer's
    display spelling of the same package) is accepted too. The rule is start_truth.bambu's.
    """
    return start_truth.bambu(live, remote).confirmed








































# The argument names are heater_ceiling's, shared with the macro run guard (#1147).
from .heater_ceiling import MACRO_BED_ARGS as _MACRO_BED_ARGS, MACRO_NOZZLE_ARGS as _MACRO_HOTEND_ARGS  # noqa: E402








def yolo_waivers(tail):
    """The `YOLO WAIVED:` lines of one print-now output, without the mark."""
    return [line.split(YOLO_WAIVED_MARK, 1)[1].strip()
            for line in str(tail or "").splitlines() if YOLO_WAIVED_MARK in line]


# Intake receipt reconciliation (#970). The chat intake leaves a sliced job's
# receipt at SLICED_NOT_STARTED until its own button flow starts it. Starting
# the same artifact out-of-band with `model_job print-now` used to leave the
# receipt there: chat could then offer to start a job already on the printer,
# and the receipt history was wrong (intake 16fd2142…, printing as print #83,
# still "not started"). A confirmed print-now start therefore moves the
# owning receipt to STARTED itself, records that the start was manual
# (started_via: print-now) and the print id, and settles the chat one-start
# claim so a later chat start of the same target is refused as a duplicate.

















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


def _print_filament_warning_packet(job_json, pending, resume):
    """Print the pending filament warnings for the operator (CLI surface).

    The CLI cannot ask interactively mid-run, so a pending warning round is
    reported as data and the job stops there; accepting is the explicit
    resume command, which records the bound acceptance first.
    """
    print(json.dumps({'status': 'FILAMENT_WARNINGS_PENDING', 'job_json': str(job_json),
                      'warnings': pending.warnings, 'resume': resume}, indent=1))
    print('Filament warnings need your acceptance before slicing. Review them, then re-run '
          'with --accept-filament-warnings to accept exactly these warnings and continue.')


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
    f.add_argument("--accept-filament-warnings", action="store_true", help="record acceptance of the job's pending filament warnings (bound to those warnings, the source and the selected plates) and continue")
    f.add_argument("--brim", type=int, default=None, help="override brim width mm (e.g. 0 to disable)")
    f.add_argument("--printer", choices=printers.MODELS, help="printer model this job is prepared for (default: [printer] model)")
    f.add_argument("--filament-slot", type=int, help="AD5X IFS feeder slot (1-4)")
    f.add_argument("--filament-color", type=str, help="filament color matching IFS slot")
    f.add_argument("--filament-material", type=str, help="filament material matching IFS slot")
    f.add_argument("--filament-spoolman-id", "--spoolman-id", dest="filament_spoolman_id", type=int, default=None, help="Spoolman spool ID")
    s = sub.add_parser("slice")
    s.add_argument("job_json")
    s.add_argument("--approved", action="store_true")
    s.add_argument("--accept-filament-warnings", action="store_true", help="record acceptance of the job's pending filament warnings (bound to those warnings, the source and the selected plates) and continue")
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
    remap.add_argument("--tool", type=str, default=None, help="logical tool index the file uses (default: the file's only used tool)")
    pn=sub.add_parser("print-now",help="bed confirmed clear: verify, upload, start, confirm, record (G-code or JOB.json)"); pn.add_argument("target")
    pn.add_argument("--accept-spool-change",action="store_true",help="operator confirmed printing with the spool now in a slot that changed since slicing (same material family only)")
    pn.add_argument("--yolo",action="store_true",help="the operator vouches for the file: report failed checks instead of refusing, no single-use approval record (the bed-clear confirmation, mechanics, axis limits and heater maximums are never waived)")
    pn.add_argument("--bed-clear-sha256",default=None,help="the operator confirmed the bed is clear for the G-code with this SHA-256 (what the chat's Start button passes)")
    pn.add_argument("--remap-slot", "--slot", dest="remap_slot", type=int, default=None, help="remap the file's one used tool to another physical IFS slot (1-4) without physical spool changes; a file using several tools is refused (use remap-slot --tool)")
    for x in (pn,f,s):
        x.add_argument("--accept-default-z-offset",action="store_true",help="the operator accepted the printer default Z offset for this start")
        x.add_argument("--reprint",action="store_true",help="the operator gave a NEW bed-clear approval to reprint the same G-code")
        x.add_argument("--bed-clear-confirmed",action="store_true",help="the operator confirms the bed is clear for this start (required to start; #1147)")
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
    if a.cmd=="print-now" and getattr(a,"yolo",False): pn_kw["yolo"]=True
    if getattr(a,"bed_clear_sha256",None): pn_kw["bed_clear"]=a.bed_clear_sha256
    elif getattr(a,"bed_clear_confirmed",False): pn_kw["bed_clear"]=True
    if a.cmd=="remap-slot":
        try:
            res = remap_slot(a.target, a.slot, a.tool)
        except ToolRemapRefused as exc:
            raise SystemExit(f"REFUSED: {exc}. Nothing changed.") from exc
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
        if getattr(a, 'accept_filament_warnings', False):
            try:
                record_filament_warning_acknowledgement(a.job_json)
            except (ValueError, OSError) as ack_error:
                raise SystemExit(f"REFUSED: {ack_error}")
        try:
            if not intake_unit_confirmation(a.job_json, units=a.units, interactive=sys.stdin.isatty()):
                return
            if not intake_color_choices(a.job_json, st, a.color_mode, a.color_slot, sys.stdin.isatty()):
                return
            if a.separate:
                _job = json.loads(Path(a.job_json).read_text()); _job["plate_mode"] = "separate"
                write_job(Path(a.job_json).parent, _job)
            out=slice_job(a.job_json, brim_override=a.brim)
        except FilamentWarningPending as pending:
            _print_filament_warning_packet(
                a.job_json, pending, f'slice {a.job_json} --approved --accept-filament-warnings')
            return
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
    intake_working_copy(path)  # canonical namespace and millimeters for everything below (#814, #825)
    job["selected_source"],job["downloaded_file"],job["audit"]=chosen,str(path),audit(path, printer_model=job.get("printer_model") or printers.current().key); write_job(folder,job)
    print("\nAUDIT: "+json.dumps(job["audit"],indent=2))
    if a.inspect_only:
        print('SOURCE_INSPECTED ' + json.dumps({'job_json': str(folder/'JOB.json'), 'sliced': False, 'uploaded': False, 'started': False}))
        return
    if not intake_unit_confirmation(folder/'JOB.json', units=a.units, interactive=sys.stdin.isatty()):
        print(f"\nSTOPPED: unit confirmation pending at {folder/'JOB.json'}. Resume with slice --approved --units mm|inch; slicing requires --approved.")
        return
    if getattr(a, 'accept_filament_warnings', False):
        try:
            record_filament_warning_acknowledgement(folder/'JOB.json')
        except (ValueError, OSError) as ack_error:
            raise SystemExit(f"REFUSED: {ack_error}")
    try:
        if not intake_color_choices(folder/'JOB.json', st, a.color_mode, a.color_slot, sys.stdin.isatty()):
            return
        out=slice_job(folder/"JOB.json", brim_override=a.brim)
    except FilamentWarningPending as pending:
        _print_filament_warning_packet(
            folder/'JOB.json', pending,
            f'fetch {a.url} --select {a.select} --approved --accept-filament-warnings' if a.select
            else 'slice ' + str(folder/'JOB.json') + ' --approved --accept-filament-warnings')
        return
    if a.print_now: print_now(out, **pn_kw)


# Code moved out of this module (#1323) still sees assignments made here
# (mock.patch, monkeypatch) for one release; see print_handy/_relocated.py.
from .. import _relocated  # noqa: E402
from ..gcode import artifact as _gcode_artifact, bounds as _gcode_bounds, verify as _gcode_verify  # noqa: E402
from ..jobs import common as _jobs_common, filament as _jobs_filament, store as _jobs_store  # noqa: E402
from ..launch import gates as _launch_gates, print_now as _launch_print_now  # noqa: E402
_relocated.keep_old_path(__name__, _gcode_bounds, _gcode_verify, _gcode_artifact, _jobs_common,
                         _jobs_store, _jobs_filament, _launch_gates, _launch_print_now,
                         live=("_native_bounds_module", "_native_bounds_broken"))
if __name__=="__main__": main()
