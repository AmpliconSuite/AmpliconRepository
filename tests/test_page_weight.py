"""
Regression tests for the plotly.js page-weight problem found 2026-08-07.

Every chart was rendered with plotly's default ``include_plotlyjs=True``, which
inlines the whole library into the HTML fragment.  Measured on production:

    sample page    3,760,780 bytes, of which 3,711,409 was plotly.js  (98.7%)
    project page   7,611,435 bytes, containing plotly.js TWICE        (97.5%)

Re-sent on every page view and uncacheable, because it was part of the HTML
rather than a static asset.  Beyond the bandwidth, a multi-megabyte response
blocks the gunicorn worker until the client drains it -- responses now fit in
the socket buffer and the worker is free immediately.

The contract these tests pin down:
  * no ``to_html()`` call embeds the library
  * both templates load it exactly once, before the figures that need it
  * the pinned CDN version matches the installed plotly package
"""

import ast
import os

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDN_PREFIX = 'https://cdn.plot.ly/plotly-'

PLOT_MODULES = (
    os.path.join(REPO_ROOT, 'caper', 'caper', 'sample_plot.py'),
    os.path.join(REPO_ROOT, 'caper', 'caper', 'StackedBarChart.py'),
    os.path.join(REPO_ROOT, 'caper', 'caper', 'summarybar.py'),
)

SAMPLE_TEMPLATE = os.path.join(REPO_ROOT, 'caper', 'templates', 'pages', 'sample.html')
PROJECT_TEMPLATE = os.path.join(REPO_ROOT, 'caper', 'templates', 'pages', 'project.html')


def _to_html_calls(path):
    """Every ``.to_html(...)`` call in a module, as AST nodes."""
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)

    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'to_html'
    ]


@pytest.mark.parametrize('module_path', PLOT_MODULES)
def test_no_figure_embeds_the_plotly_library(module_path):
    """include_plotlyjs must be explicitly False at every call site."""
    calls = _to_html_calls(module_path)
    assert calls, f"no to_html() calls found in {module_path} -- has it been renamed?"

    for call in calls:
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        assert 'include_plotlyjs' in kwargs, (
            f"{os.path.basename(module_path)}:{call.lineno} calls to_html() without "
            f"include_plotlyjs. The default is True, which inlines 3.7 MB of "
            f"plotly.js into the response."
        )
        value = kwargs['include_plotlyjs']
        assert isinstance(value, ast.Constant) and value.value is False, (
            f"{os.path.basename(module_path)}:{call.lineno} passes "
            f"include_plotlyjs={ast.dump(value)}; it must be False, with the "
            f"library loaded once by the template."
        )


@pytest.mark.parametrize('template_path,figure_marker', (
    (SAMPLE_TEMPLATE, '{{ graph|safe }}'),
    (PROJECT_TEMPLATE, '{{ stackedbar_graph|safe }}'),
))
def test_template_loads_plotly_once_before_the_figures(template_path, figure_marker):
    """One <script src>, and it has to come before the figure it serves.

    The fragment plotly emits calls Plotly.newPlot inline, as the page parses,
    so a tag placed after it -- or in a bottom-of-body block -- renders a blank
    chart and a console error.
    """
    with open(template_path) as fh:
        body = fh.read()

    tags = [line for line in body.splitlines() if CDN_PREFIX in line]
    assert len(tags) == 1, (
        f"{os.path.basename(template_path)} has {len(tags)} plotly.js script "
        f"tags; expected exactly 1"
    )

    assert figure_marker in body, (
        f"{figure_marker} is gone from {os.path.basename(template_path)} -- this "
        f"test's ordering check needs updating"
    )
    assert body.index(CDN_PREFIX) < body.index(figure_marker), (
        f"the plotly.js tag in {os.path.basename(template_path)} comes after "
        f"{figure_marker}; the figure's inline Plotly.newPlot call would run first"
    )


@pytest.mark.parametrize('template_path', (SAMPLE_TEMPLATE, PROJECT_TEMPLATE))
def test_pinned_cdn_version_matches_the_installed_package(template_path):
    """Upgrading the plotly package must not silently desync the templates.

    A mismatch is not a crash -- the charts render against whatever version the
    CDN serves -- which is exactly why it needs a test.
    """
    from plotly.offline import get_plotlyjs_version

    with open(template_path) as fh:
        body = fh.read()

    start = body.index(CDN_PREFIX) + len(CDN_PREFIX)
    pinned = body[start:body.index('.min.js', start)]

    assert pinned == get_plotlyjs_version(), (
        f"{os.path.basename(template_path)} pins plotly.js {pinned} but the "
        f"installed plotly package bundles {get_plotlyjs_version()}. Update the "
        f"<script src> in both templates."
    )
