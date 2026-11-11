"""The one model-source registry: every surface reads the same list."""
import re

from print_handy.core import link_intake, model_job, model_sources, site_accounts
from print_handy.webui import sources as sources_page


def test_registry_covers_the_nine_sites_plus_direct_and_dropbox():
    sites = {s.site for s in model_sources.SOURCES}
    assert sites == {"makerworld", "printables", "thingiverse", "myminifactory",
                     "cults3d", "crealitycloud", "thangs", "nexprint",
                     "makeronline", "grabcad"}
    assert model_sources.DIRECT.site == "direct"
    assert ".stl" in model_sources.DIRECT_FILE_EXTS
    assert ".3mf" in model_sources.DIRECT_FILE_EXTS


def test_model_job_dispatch_tables_come_from_the_registry():
    assert model_job.SITE_HOSTS == model_sources.hosts_map()
    assert model_job.SITE_INVENTORY_FUNCS == model_sources.inventory_funcs()
    assert model_job.SITE_DOWNLOAD_FUNCS == model_sources.download_funcs()
    # GrabCAD is listed as not supported and is never dispatchable.
    assert "grabcad" not in model_job.SITE_HOSTS
    assert "grabcad" not in model_job.SITE_INVENTORY_FUNCS


def test_supported_links_message_is_the_registry_sentence():
    assert model_job.SUPPORTED_LINKS_MESSAGE == (
        "supported links: MakerWorld, Printables, Thingiverse, MyMiniFactory, "
        "Cults3D, Creality Cloud, Thangs, Nexprint, MakerOnline model pages, "
        "or a direct link to a .stl/.3mf file (a Dropbox share link works too)")


def test_source_report_tuples_come_from_the_registry():
    assert site_accounts.SOURCES == tuple(
        (s.site, s.label, s.kind, s.note) for s in model_sources.SOURCES)


def test_link_recognition_matches_the_registry_patterns():
    examples = {
        "makerworld": "https://makerworld.com/en/models/1-x",
        "printables": "https://www.printables.com/model/1-x",
        "thingiverse": "https://www.thingiverse.com/thing:42",
        "myminifactory": "https://www.myminifactory.com/object/x-42",
        "cults3d": "https://cults3d.com/en/3d-model/tool/x",
        "crealitycloud": "https://www.crealitycloud.com/model-detail/abc",
        "thangs": "https://thangs.com/m/42",
        "nexprint": "https://www.nexprint.com/models/AbC42",
        "makeronline": "https://www.makeronline.com/model/x/42.html",
    }
    for site, url in examples.items():
        assert link_intake.model_link(url) == url, site
        host = re.sub(r"^https?://", "", url).split("/")[0]
        assert model_sources.site_for_host(host) == site, site
    # A site's non-model page and the not-supported site are not intake.
    assert link_intake.model_link("https://www.printables.com/about") is None
    assert link_intake.model_link("https://grabcad.com/library/x-1") is None


def test_sources_page_lists_every_registry_entry(monkeypatch):
    monkeypatch.setattr(site_accounts, "credential_state", lambda site: "missing")
    html = sources_page.render_sources_page()
    for source in model_sources.SOURCES:
        assert source.label in html
    assert "Direct file link" in html
    assert "Dropbox" in html


def test_sources_page_status_uses_the_real_credential_state(monkeypatch, tmp_path):
    # Regression (review of #1100): the page test above stubs
    # credential_state, so the real function never ran for the listing
    # sites. MakerOnline has no saved credential at all, and the real
    # credential_state raised KeyError for it, so the page failed.
    monkeypatch.setattr(site_accounts, "token_path", lambda site: tmp_path / site)
    for account in site_accounts.LOGIN_SITES.values():
        monkeypatch.delenv(account.env_name, raising=False)
    status = {row[0]: row[3] for row in sources_page.sources_rows()}
    assert status["MakerOnline"] == "No key needed"
    assert status["Cults3D"] == "Needs your API key"
    assert status["Thingiverse"] == "Not connected"
    assert status["Printables"] == "Ready"
    html = sources_page.render_sources_page()
    for source in model_sources.SOURCES:
        assert source.label in html
