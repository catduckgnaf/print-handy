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


from ..jobs.slice import (  # moved in 2.3.0 (#1323); old path kept for one release
    BED_LIMIT_MM, INCH_TO_MM, ImplicitPlateSplit, NoPrintableVolume, PAINTED_SCALE_STEPS,
    UNIT_QUESTION_MAX_MM, _choice_label, _edit_kw, _enforce_filament_readiness,
    _enforce_filament_warnings, _fallback_selection, _filament_ack_covers, _gcode_object_count,
    _is_generated_mm, _job_fit, _merge_edge, _painted_fit, _painted_fit_for_job, _painted_tower_mm,
    _part_slots, _per_part_view, _placed_bounds_by_object, _plates_for,
    _rearranged_group_placement, _slice_full_size_split, _slice_merged, _slice_merged_multi_tool,
    _slice_painted_plates, _slice_per_plate, _support_kw, _whole_source_usage_warnings,
    copies_supported, filament_fallback_slot, filament_fallbacks, finalize_multicolor,
    full_size_one_plate, model_info_output, normalize_units, painted_fit, painted_fit_for_job,
    painted_plate_layout, painted_plate_scale, per_part_fit_options, plate_manifest,
    prepare_first_layer, refuse_assembly_split, single_color_groups, slice_job, slice_multi_plate,
)










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


from ..jobs.store import (  # moved in 2.3.0 (#1323); old path kept for one release
    _recipe_binding, intake_folder, intake_id_from_message, record_support_decision, safe_slug,
    save_filament_selection, set_color_decisions, stamp_job_printer,
)




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









def _audit_dims(job):
    """Authored extents from the job audit, or None when they were not measured."""
    ad = job.get('audit') or {}
    try:
        dims = tuple(float(ad[key]) for key in ('size_x', 'size_y', 'size_z'))
    except (KeyError, TypeError, ValueError):
        return None
    return dims if all(value > 0 for value in dims) else None




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
from ..jobs import slice as _jobs_slice  # noqa: E402
from ..launch import gates as _launch_gates, print_now as _launch_print_now  # noqa: E402
_relocated.keep_old_path(__name__, _gcode_bounds, _gcode_verify, _gcode_artifact, _jobs_common,
                         _jobs_store, _jobs_filament, _launch_gates, _launch_print_now, _jobs_slice,
                         live=("_native_bounds_module", "_native_bounds_broken"))
if __name__=="__main__": main()
