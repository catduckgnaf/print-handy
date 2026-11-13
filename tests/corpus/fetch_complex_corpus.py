"""Fetch the #458 complex corpus models into a local corpus root.

Storage decision (see tests/corpus/complex/README.md): the model files are
tens of MB each and are NOT committed to this repository. This script downloads
them, pinned by the SHA256 and byte size recorded in
tests/corpus/complex_cases.toml, into a root outside the repo (default
~/print-handy-complex-corpus) laid out as one folder per case:

    <root>/<slug>/model.stl|model.3mf   the flow-run primary file
    <root>/<slug>/<extra files>         measured companion files, where any
    <root>/<slug>/case.toml             clicks + expectations for the harness
    <root>/<slug>/ATTRIBUTION           title, author, source, licence

Downloads use the same eligible-site paths as production intake (#462):
Printables through its GraphQL API (the getDownloadLink mutation
print_handy.core.model_job.printables_download uses; no token), NASA through
its direct public file link, MakerWorld through the account-token API call
print_handy.core.model_job.maker_download uses (~/.config/bambu/token). Private-tier cases are licence-flagged for local
runs only: they are skipped unless --private-root is given, and they are never
written into the public root.

Usage:
    python tests/corpus/fetch_complex_corpus.py [--root DIR] [--private-root DIR] [--only slug,...]
"""
import argparse
import hashlib
import json
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "complex_cases.toml"
NASA_STL = ("https://assets.science.nasa.gov/content/dam/science/cds/3d/"
            "resources/printable/curiosity-rover-(simplified)/Curiosity%20(200uM).stl")
PRINTABLES_GQL = "https://api.printables.com/graphql/"
LINK_MUTATION = """mutation GetDownloadLink($id: ID!, $printId: ID!, $source: DownloadSourceEnum!,
$fileType: DownloadFileTypeEnum!) { getDownloadLink(id: $id, printId: $printId, source: $source,
fileType: $fileType) { ok output { link } errors { field messages } } }"""

# The generic answers every complex case starts from (same set as
# run_external_corpus.py), plus the per-case clicks recorded in CASE_CLICKS.
# The harness answers a question with the FIRST [[click]] rule that matches
# it, so case_toml() writes a case's own rules before these generic ones:
# a case can override a generic answer (per-part colours instead of "One
# color for everything", "100% · rearrange 1 plate" instead of "Shrink to").
GENERIC_CLICKS = [
    {"question": "Choose the color", "button": "White"},
    {"question": "one color for everything?", "button": "One color for everything"},
    {"question": "does not fit the bed full size", "button": "Shrink to"},
    # Multi-plate designer projects (#1373 archive cases): the full-size split, then plate 1 first.
    {"question": "full size as one plate", "button": "split"},
    {"question": "Which plate prints first?", "button": "Plate 1"},
    {"question": "How many copies", "button": "One copy"},
    {"question": "·", "button": "Slice and print"},
]
# slug -> [[click]] rules that take precedence over GENERIC_CLICKS. Button
# text matches the fixture state's slot labels (tests/pipeline/fixtures/
# printer_hardware.json: 1 white, 2 pink, 3 black, 4 dark green PLA).
CASE_CLICKS = {
    # Live failure on 2.1.9 (2026-10-29): per-part colours + the fit
    # planner's "100% · rearrange 1 plate" sliced the authored layout and
    # failed "part group does not fit the target bed without cropping". The
    # colours mirror that live attempt's slot split (archived JOB.json): every
    # part on slot 1 except double-frame on slot 4, so the 8-part slot-1 group
    # keeps the authored 239 mm layout. Part questions are matched by
    # "<part>, " (the name before its measured size) so they answer both the
    # grouped ("Choose one color/material for both framelegs: frameLeg,
    # 95.4 x ...") and the individual ("Choose slot/color/material for
    # frameLeg, 95.4 x ...") wording.
    "duplo-swing-two-seats": [
        {"question": "one color for everything?", "button": "Choose colors"},
        # Match on the group names, not the full question text: the questions
        # are worded "for both framelegs (95.4 x ...)", "for all 4 chains (...)".
        {"question": "framelegs", "button": "White"},
        {"question": "double-frame", "button": "Dark green"},
        {"question": "seat-bricks", "button": "White"},
        {"question": "chains", "button": "White"},
        # Since #601 the one-plate rearrangement is applied without asking;
        # this rule only answers if the fit question comes back.
        {"question": "bed full size", "button": "rearrange 1 plate"},
        # On a failed slice, stop rather than retry so the receipt keeps the error.
        {"question": "No G-code was kept", "button": "Cancel"},
    ],
    # Live #1373 (2026-11-12, 2.3.1): per-part colors, every part on one slot,
    # as the live attempt answered. The supports menu must not appear at all,
    # so no rule answers it; an unanswered question fails the case.
    "pinecone-cottage-lantern": [
        {"question": "one color for everything?", "button": "Choose colors"},
        {"question": "Lantern Lower Section", "button": "Pink"},
        {"question": "Pinecone Lantern Base", "button": "Pink"},
        {"question": "Pinecone Lantern Hanging Loop", "button": "Pink"},
        {"question": "Lantern Upper Chimney Section", "button": "Pink"},
    ],
}
# Verified 2026-10-27 (mock-tier flow runs): these three cases exceed the
# product's own intake limits, so the honest pinned expectation is the
# refusal receipt - the corpus case documents the limit behaviour.
CASE_EXPECT = {
    # The refusal cases pin the refusal reason too (P2-177): any FAILED
    # receipt — including one from an unrelated intake regression — must
    # not satisfy them. The texts are intake_resources' refusal messages.
    "fnaf-mask": {"status": "FAILED", "error_contains": "STL exceeds 32 MiB limit"},
    "gyroid-surface": {"status": "FAILED", "error_contains": "STL exceeds 32 MiB limit"},
    "chinese-dragon": {"status": "FAILED", "error_contains": "model XML exceeds 400 MiB limit"},
    # What a working flow must do for the live 2026-10-29 failure: start one
    # full-size print with the chosen per-part colors on one rearranged plate.
    # Failed on 2.1.9; fixed by #601 (passes on main since).
    # #1373: a designer no-supports project whose scales overhang their own
    # mesh. The pre-check finds no clearance risk, the menu offers no
    # supports, repair or AI fix, and the slice prints without supports. The
    # harness's conversation checks (harness.conversation_checks) also run.
    "pinecone-cottage-lantern": {
        "status": "STARTED", "scale": 1.0, "gcodes": 1, "supports": False,
        "clearance_codes": [],
        "buttons_absent": ["with supports", "Extend underside", "Flatten (sink)", "Reorient",
                           "Fix this for my printer"],
    },
    "duplo-swing-two-seats": {
        "status": "STARTED", "scale": 1.0, "gcodes": 1,
        "colors": {"frameLeg": "white", "double-frame": "dark green",
                   "seat-brick": "white", "chain": "white"},
    },
}


def _open_direct(url, data=None, headers=None):
    """Open a URL without the ambient proxy (Printables' API drops proxied
    requests from some networks); retry a few times on connection errors."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=data, headers=headers or {})
    last = None
    for attempt in range(3):
        try:
            return opener.open(req, timeout=120)
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last


def printables_link(file_id, model_id):
    payload = json.dumps({"operationName": "GetDownloadLink",
                          "variables": {"id": str(file_id), "printId": str(model_id),
                                        "source": "model_detail", "fileType": "stl"},
                          "query": LINK_MUTATION}).encode()
    # The read is inside the retry: a connection can open fine and still
    # truncate the (small) GraphQL response mid-body, which raises
    # http.client.IncompleteRead on resp.read() - retry the whole
    # open-and-read, bounded like _open_direct, instead of crashing.
    data = None
    for attempt in range(3):
        try:
            with _open_direct(PRINTABLES_GQL, data=payload,
                              headers={"Content-Type": "application/json",
                                       "User-Agent": "Mozilla/5.0 Chrome/126",
                                       "Referer": "https://www.printables.com/"}) as resp:
                data = json.loads(resp.read().decode())
            break
        except Exception:  # noqa: BLE001 - retried, then re-raised
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    link = data["data"]["getDownloadLink"]
    if not link.get("ok"):
        raise RuntimeError(f"Printables refused a download link: {link.get('errors')}")
    return link["output"]["link"]


def makerworld_link(instance_id):
    """The signed file URL for one MakerWorld print profile, through the same
    API call and account token production intake uses
    (print_handy.core.model_job.maker_download / maker_headers; the token is
    ~/.config/bambu/token). Without the token this raises the product's own
    "needs an account token" error. A 418 is MakerWorld's robot check: it is
    reported, never solved or worked around."""
    import urllib.error
    sys.path.insert(0, str(HERE.parents[1]))
    from print_handy.core import model_job
    try:
        data = model_job.request_json(
            f"https://api.bambulab.com/v1/design-service/instance/{instance_id}/f3mf?type=download",
            headers=model_job.maker_headers())
    except urllib.error.HTTPError as exc:
        if exc.code == 418:
            raise RuntimeError("MakerWorld answered with a robot check (418); not bypassed - "
                               "place the file in the private root by hand") from None
        raise
    return data["url"]


def download(url, dest):
    """Download to dest. Some CDNs drop long direct connections mid-file, so
    a short read resumes with an HTTP Range request instead of restarting -
    and a short file is never mistaken for a finished one."""
    written = 0
    expected = None
    stalled = 0
    for _attempt in range(60):
        before = written
        headers = {"User-Agent": "Mozilla/5.0 Chrome/126"}
        if written:
            headers["Range"] = f"bytes={written}-"
        try:
            with _open_direct(url, headers=headers) as resp:
                if written and resp.status != 206:  # Range ignored: start over
                    written = 0
                if expected is None:
                    total = resp.headers.get("Content-Range", "").rsplit("/", 1)[-1]
                    expected = int(total) if total.isdigit() else (
                        int(resp.headers["Content-Length"]) if resp.headers.get("Content-Length") else None)
                with open(dest, "ab" if written else "wb") as out:
                    while chunk := resp.read(1 << 20):
                        out.write(chunk)
                        written += len(chunk)
        except OSError:
            pass  # dropped mid-read: resume from what is on disk
        if expected is None or written >= expected:
            return
        stalled = stalled + 1 if written == before else 0
        if stalled >= 8:
            break
        print(f"  connection dropped at {written} of {expected} bytes, resuming")
    raise RuntimeError(f"{dest}: download kept truncating at {written} bytes")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_file(case, file_id, dest, want_bytes, want_sha):
    if dest.is_file() and dest.stat().st_size == want_bytes and sha256(dest) == want_sha:
        print(f"  ok (already fetched) {dest.name} {want_bytes}")
        return
    if case["site"] == "printables":
        url = printables_link(file_id, case["site_model_id"])
    elif case["site"] == "nasa":
        url = NASA_STL
    elif case["site"] == "makerworld":
        url = makerworld_link(case["site_instance_id"])
    else:
        raise RuntimeError(f"no fetch route for site {case['site']}")
    download(url, dest)
    got = dest.stat().st_size
    if got != want_bytes or sha256(dest) != want_sha:
        raise RuntimeError(f"{dest}: fetched {got} bytes / sha mismatch - refusing a changed file")
    print(f"  fetched {dest.name} {got}")


def _toml_value(value):
    """A TOML literal for an [expect] value. JSON covers strings, numbers,
    booleans and arrays; a table (e.g. colors = {part: color}) must be a TOML
    inline table, which JSON's "key": value syntax is not."""
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(str(k))} = {_toml_value(v)}"
                                for k, v in value.items()) + " }"
    return json.dumps(value)


def case_toml(case):
    # Case rules first: the harness takes the first matching rule.
    clicks = CASE_CLICKS.get(case["slug"], []) + GENERIC_CLICKS
    lines = [f"# Complex corpus case for #458 - fetched, not committed. See complex_cases.toml.",
             f"license = {json.dumps(case['license'])}",
             f"fixture = {json.dumps(case['file'])}",
             f"description = {json.dumps(case['title'] + ' - target ' + case['target_domain'] + ' / ' + case['target_printer'])}",
             ""]
    for rule in clicks:
        lines += ["[[click]]", f"question = {json.dumps(rule['question'])}",
                  f"button = {json.dumps(rule['button'])}", ""]
    expect = {"status": "STARTED", **CASE_EXPECT.get(case["slug"], {})}
    lines.append("[expect]")
    for key, value in expect.items():
        lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path.home() / "print-handy-complex-corpus"),
                        help="public corpus root (outside the repository)")
    parser.add_argument("--private-root", default=None,
                        help="also fetch private-tier cases into this separate root")
    parser.add_argument("--only", default=None, help="comma-separated slugs")
    args = parser.parse_args()
    only = set(args.only.split(",")) if args.only else None
    manifest = tomllib.loads(MANIFEST.read_text())
    extras = {}
    for row in manifest.get("case.extra_file", []):
        pass  # tomllib nests these under each case; handled below
    for case in manifest["case"]:
        tier = case["tier"]
        if only and case["slug"] not in only:
            continue
        if case["status"] != "ready":
            print(f"SKIP {case['slug']}: {case['status']} - {case.get('blocked_reason', '')[:100]}")
            continue
        if tier == "private" and not args.private_root:
            print(f"SKIP {case['slug']}: private tier (pass --private-root to fetch it locally)")
            continue
        root = Path(args.private_root if tier == "private" else args.root)
        folder = root / case["slug"]
        folder.mkdir(parents=True, exist_ok=True)
        print(f"{case['slug']} -> {folder}")
        fetch_file(case, case.get("site_file_id"), folder / case["file"],
                   case["bytes"], case["sha256"])
        for extra in case.get("extra_file", []):
            fetch_file(case, extra["site_file_id"], folder / extra["name"],
                       extra["bytes"], extra["sha256"])
        (folder / "case.toml").write_text(case_toml(case))
        (folder / "ATTRIBUTION").write_text(
            f"{case['title']} by {case['author']}\nSource: {case['source_page']}\n"
            f"License: {case['license']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
