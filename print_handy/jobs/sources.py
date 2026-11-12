"""Model sources: inventory and download for every supported site (MakerWorld, Printables,
Thingiverse, MyMiniFactory, Cults3D, Creality Cloud, Thangs, Nexprint, MakerOnline) and direct
file links, with the public-address and redirect guards.

Moved verbatim from ``print_handy.core.model_job`` (#1323). The old names are still
importable from there for one release.
"""
import base64, html, ipaddress, json, os, re, shutil, socket, time, zipfile
import http.client
import urllib.request
from pathlib import Path

from ..core import model_sources as _model_sources
from ..core.site_accounts import SiteLoginNeeded
from .common import ARCHIVE, UA, request_json
from .store import safe_slug


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
    from ..core.intake_resources import save_download
    opener=urllib.request.build_opener(_DropAuthOnHostChange) if "Authorization" in h else None
    with (opener.open(req, timeout=90) if opener else urllib.request.urlopen(req, timeout=90)) as r:
        save_download(r, target)
    if target.stat().st_size == 0: raise RuntimeError("downloaded an empty file")


def maker_headers():
    # The token file, else MAKERWORLD_TOKEN (site_accounts.stored).
    from ..core.site_accounts import stored
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
        from ..core.makerworld_profiles import annotate as annotate_maker_profiles
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
    from ..core.makerworld_profiles import annotate as annotate_maker_profiles
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
    from ..core.site_accounts import stored
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
        from ..core.site_accounts import read_token
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
            from ..core.site_accounts import refused
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
    from ..core.site_accounts import read_token, refused
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
    from ..core.site_accounts import stored
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
    from ..core.site_accounts import read_token
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
            from ..core.intake_resources import CHUNK, MAX_DOWNLOAD_BYTES, MIB, _refuse
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
    from ..core.site_accounts import read_token, refused
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
            from ..core.site_accounts import refused
            raise refused("nexprint")
        raise RuntimeError(f"Nexprint answered with an unexpected response (HTTP {status}), "
                           "so the request was not followed")
    code = d.get("code")
    if code in (0, 200): return d.get("data")
    if code in (401, 902):
        from ..core.site_accounts import refused
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
    from ..core.site_accounts import read_token
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
        from ..core.intake_resources import save_download
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
