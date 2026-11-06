"""Corpus items get the 600 s timeout marker; the rest of the suite keeps the pyproject.toml 120 s."""


def test_corpus_item_has_the_600_second_timeout(request):
    marker = request.node.get_closest_marker("timeout")
    assert marker is not None
    assert marker.args == (600,)
