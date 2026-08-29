"""Regression tests for project-page template behavior."""

from pathlib import Path

from django.template.loader import render_to_string


TEMPLATE_DIR = (
    Path(__file__).resolve().parents[1] / "caper" / "templates" / "pages"
)


def test_loading_page_uses_requested_processing_message():
    source = (TEMPLATE_DIR / "loading.html").read_text()

    assert (
        "This project is being processed, please wait. "
        "The page will automatically refresh in 15 seconds ..."
    ) in source


def test_project_archive_inputs_accept_zip_and_edit_drop_zone_selects_files():
    sources = [
        (TEMPLATE_DIR / template_name).read_text()
        for template_name in ("create_project.html", "edit_project.html")
    ]

    for source in sources:
        assert 'accept=".tar.gz,.zip"' in source
        assert 'const validFileExtensions = [".tar.gz", ".zip"];' in source
        assert "validFileExtensions.some(extension =>" in source
        assert "Please select .tar.gz or .zip files only." in source
        assert (
            'href="https://docs.ampliconrepository.org/en/latest/getting-started/"'
            in source
        )

    source = sources[1]

    assert "fileDropArea.addEventListener('dragover'" in source
    assert "fileDropArea.addEventListener('drop'" in source
    assert "event.preventDefault();" in source
    assert "input.files = event.dataTransfer.files;" in source
    assert (
        "input.dispatchEvent(new Event('change', { bubbles: true }));" in source
    )


def test_sample_amplicon_table_has_continuation_footer():
    source = (TEMPLATE_DIR / "sample.html").read_text()

    assert 'id="amplicon-continuation"' in source
    assert 'id="amplicon-continuation-summary"' in source
    assert 'id="amplicon-next-page"' in source
    assert 'aria-live="polite"' in source
    assert 'aria-controls="myTable2"' in source
    assert "table.page.info()" in source
    assert "table.page('next').draw('page')" in source
    assert "remaining > 0" in source
    assert source.index('id="amplicon-next-page"') < source.index(
        'id="amplicon-continuation-summary"'
    )


def test_home_project_descriptions_expand_inline_and_remain_searchable():
    source = (TEMPLATE_DIR / "index.html").read_text()

    assert source.count(
        "{% include 'includes/project_description_cell.html' %}"
    ) == 3
    assert "$('#unifiedProjectTable').on(" in source
    assert "'.project-description-toggle'" in source
    assert "button.attr('aria-expanded'" in source
    assert "'aria-label'," in source
    # The toggle opens the row it sits in rather than going anywhere, so it is
    # ink with a chevron, not blue. Its focus ring is the page's blue and not
    # Bootstrap's, which is the last place #007bff was hiding.
    # Anchored on the standalone rule: .home-projects scopes an earlier one.
    toggle = source[source.index("\n    .project-description-toggle {"):]
    toggle = toggle[: toggle.index("focus-visible") + 200]
    assert "color: #414042;" in toggle
    assert "#1f66d0" not in toggle
    assert "#12489b" not in toggle
    assert "rgba(0, 123, 255" not in source

    description_cell = (
        TEMPLATE_DIR.parent / "includes" / "project_description_cell.html"
    ).read_text()
    assert 'data-search="{{ project.description }}"' in description_cell
    assert "project.description|truncatechars:100" in description_cell
    assert "{{ project.description }}" in description_cell
    assert 'class="project-description-toggle"' in description_cell
    assert 'type="button"' in description_cell
    assert 'aria-expanded="false"' in description_cell
    assert 'data-project-name="{{ project.project_name }}"' in description_cell
    assert ">More<" in description_cell


def test_home_project_rows_distinguish_featured_from_public_and_private():
    """Featured and private rows are marked; a plain public row is the default.

    The redesign dropped the "Public" badge -- a tag on two thirds of the rows
    is not information. Featured is the site's own emphasis and applies to the
    whole row, so it is the row that carries it, silently; CoRAL is a fact about
    the data, so it keeps a word.
    """
    source = (TEMPLATE_DIR / "index.html").read_text()

    assert '<span class="home-chip home-chip-private">Private</span>' in source
    assert '<span class="badge badge-primary">Public</span>' not in source

    # Featured says so with a rule down the edge of the row and a tint, not with
    # the word repeated down a column. The rule is the hover target, so it has to
    # say what it means to anyone who reaches it by pointer or by screen reader.
    assert 'class="home-featured-rule"' in source
    assert 'data-tip="Featured project"' in source
    assert 'aria-label="Featured project"' in source
    assert ".home-featured-rule {" in source
    assert "tr.home-featured td {" in source
    assert "table.home-projects td:first-child {" in source

    # CoRAL is a chip beside the name, in all three row loops, and written the
    # way the tool is written rather than upper-cased by CSS.
    assert source.count('class="home-chip home-chip-coral"') == 3
    assert ">CoRAL</span>" in source
    assert ".home-chip-coral    { background: #f0a202; color: #3d2f05; }" in source
    assert ".home-chip-private  { background: #f0f1f3; color: #4a4d52; }" in source


def test_home_project_rows_are_all_the_same_height():
    """Otherwise the table is a different height on every page.

    A description that wraps makes its row taller, and each page has a different
    number of those, so the pager moved up and down as you paged through -- the
    control you were aiming at was never twice in the same place. The fix is the
    collapsed description occupying exactly one line, not padding every row out
    to the height of the tallest.
    """
    source = (TEMPLATE_DIR / "index.html").read_text()

    assert ".home-projects .project-description {" in source
    assert "white-space: nowrap;" in source
    assert ".home-projects .project-description.is-expanded .project-description-full {" in source
    assert "white-space: normal;" in source
    assert "table.home-projects tbody td {" in source
    assert "height: 35px;" in source


def test_home_page_type_stays_on_one_scale():
    """Six sizes and two tracking values, or the page stops looking set.

    The home page had fourteen sizes, several of them half a pixel apart, and
    six different letter-spacings -- differences too small to mean anything but
    large enough to make the page look assembled from parts. Anything new here
    should land on a step that already exists; adding a step is a decision, and
    this test is where it gets made rather than drifted into.
    """
    import re
    from collections import Counter

    source = (TEMPLATE_DIR / "index.html").read_text()
    css = source[: source.index("{% endblock %}")]

    scale = {"27px", "16px", "15px", "13.5px", "12.5px", "11px"}
    # Sized against the text they sit in, not against the page: the disclosure
    # caret and the description's More button.
    glyphs = {"8px", "0.86em", "0.72em"}

    sizes = Counter(re.findall(r"font-size:\s*([^;]+);", css))
    assert set(sizes) <= scale | glyphs, f"off-scale: {set(sizes) - scale - glyphs}"

    # -0.02em closes up the display figures; .08em is uppercase micro-labels.
    # Mixed-case text is not tracked.
    tracking = Counter(re.findall(r"letter-spacing:\s*([^;]+);", css))
    assert set(tracking) == {"-0.02em", ".08em"}

    # The project name is the row's subject, said with weight at the size of the
    # row rather than with a size of its own.
    name_rule = css[css.index(".home-pname {"):]
    name_rule = name_rule[: name_rule.index("}")]
    assert "font-weight: 700;" in name_rule
    assert "font-size" not in name_rule


def test_home_page_blue_means_one_thing():
    """Three blues, each with one job, and none of them Bootstrap's.

    The page had accumulated eight: our link blue and its hover, a border blue
    and a second border blue, a tint and a text blue for the Release tag, plus
    Bootstrap's #007bff and its hover on everything we had not styled by hand
    (Advanced, the citation, every footer link). Blue that also means "release"
    or "heading" stops meaning "you can follow this", which is the only meaning
    it has to keep.
    """
    import re

    source = (TEMPLATE_DIR / "index.html").read_text()
    css = source[: source.index("{% endblock %}")]
    # The palette note at the top of the block names the retired values.
    rules = css[css.index(".home-band {"):]

    blues = {
        value.lower()
        # Six digits first: the alternation is ordered, and #1f66d0 would
        # otherwise match as #1f6 and be read as a green.
        for value in re.findall(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", rules)
        if _is_blue(value)
    }
    assert blues == {"#1f66d0", "#12489b", "#5894f4"}, blues

    # Bootstrap's default link colour is replaced once, site-wide, rather than
    # per-element: otherwise the next unstyled link brings #007bff back.
    site_css = (
        Path(__file__).resolve().parents[1] / "caper" / "static" / "css" / "style.css"
    ).read_text()
    assert "\na {\n    color: #1f66d0;\n}" in site_css
    assert "color: #12489b;" in site_css
    assert "#5391F4" not in site_css

    # The two update tags are the same category of thing, so the same grey.
    assert ".home-tag-release,\n    .home-tag-paper { background: #f0f1f3;" in rules

    # Blue leaves the page; ink acts on it. The example pills are the filter
    # pills' twin and take their colour and size, and the pager moves the table
    # in place, so neither is blue -- only the Search button's fill is.
    examples = rules[rules.index(".home-ex {"):]
    examples = examples[: examples.index("}")]
    assert "color: #6a6d73;" in examples
    assert "font-size: 12.5px;" in examples

    pager = rules[rules.index(".paginate_button.previous,"):]
    pager = pager[: pager.index(".paginate_button.current")]
    assert "#1f66d0" not in pager
    assert "color: #414042 !important;" in pager

    # News titles carry the arrow, not the colour.
    title_rule = rules[rules.index("a.home-news-title {"):]
    title_rule = title_rule[: title_rule.index("}")]
    assert "color: #414042;" in title_rule
    assert 'content: "\\00a0\\2192";' in rules


def _is_blue(value):
    """True when the hex is blue-dominant by a clear margin."""
    if len(value) == 4:
        value = "#" + "".join(char * 2 for char in value[1:])
    red, green, blue = (int(value[index:index + 2], 16) for index in (1, 3, 5))
    return blue > red + 40 and blue > green + 40


def test_home_project_counts_sit_in_the_filter_band_without_a_heading():
    """The table says it is the projects; only the counts add anything.

    A "PROJECTS" label over a table of projects is a caption for something
    already obvious, and the figures band above already gives the public total.
    The counts move down into the band with the filter, where they qualify what
    the filter is searching.
    """
    source = (TEMPLATE_DIR / "index.html").read_text()

    assert 'class="home-counts"' in source
    assert ".home-counts {" in source
    assert '<div class="home-eyebrow">\n            Projects' not in source

    band = source.index('class="home-table-head"')
    counts = source.index('class="home-counts"')
    table = source.index('id="unifiedProjectTable"')
    assert band < counts < table

    # The private count is still explained, and still only shown to someone who
    # has private projects to be counted.
    assert "{% if user.is_authenticated %}" in source
    assert "{{ private_projects|length }} private" in source


def test_home_project_description_toggle_only_renders_for_long_text():
    long_description = "A" * 101
    long_html = render_to_string(
        "includes/project_description_cell.html",
        {"project": {"description": long_description, "project_name": "Long"}},
    )
    short_html = render_to_string(
        "includes/project_description_cell.html",
        {"project": {"description": "A" * 100, "project_name": "Short"}},
    )

    assert long_description in long_html
    assert "project-description-toggle" in long_html
    assert "project-description-toggle" not in short_html


def test_home_figure_disclosures_point_at_panels_that_exist():
    """Every figure that offers a breakdown must open one.

    The buttons and the panels are matched by hand-written ids, and a typo in
    either produces a caret that opens nothing -- which looks like a broken
    page but throws no error, so nothing else would catch it.
    """
    import re

    source = (TEMPLATE_DIR / "index.html").read_text()

    panels = set(re.findall(r'class="home-breakdown" id="([\w-]+)"', source))
    targets = set(re.findall(r'data-panel="([\w-]+)"', source))
    controls = set(re.findall(r'aria-controls="([\w-]+)"', source))

    assert targets, "no figure disclosures found"
    assert targets <= panels, f"buttons point at missing panels: {targets - panels}"
    assert panels <= targets, f"panels no button opens: {panels - targets}"
    # A screen reader needs the same relationship the click handler uses.
    assert targets <= controls


def test_home_table_pager_is_moved_out_of_the_scrolling_wrapper():
    """The row count and the pager belong together, and outside the scroller.

    Inside .home-table-scroll they scroll sideways out of view on a narrow
    screen, which is most of why the table looked like it had no more rows.
    """
    source = (TEMPLATE_DIR / "index.html").read_text()

    assert 'id="home-table-foot"' in source
    assert "$('#unifiedProjectTable_info, #unifiedProjectTable_paginate')" in source
    assert ".detach().appendTo('#home-table-foot')" in source
    # The count names its unit and the pager is arrowed, so the footer reads as
    # a control rather than as a caption.
    assert 'info: "Showing _START_ to _END_ of <b>_TOTAL_</b> projects"' in source
    assert 'next: "Next \\u2192"' in source

    foot_marker = source.index('id="home-table-foot"')
    scroll_close = source.index('id="unifiedProjectTable"')
    assert foot_marker > scroll_close


def test_project_sample_table_refits_horizontal_scroller_after_reveal():
    """The hidden table's intrinsic width must not survive its reveal.

    DataTables puts both the wide sample table and its controls in one wrapper.
    The wrapper must be allowed to fit the viewport, then the table's stale
    inline pixel width must be reset before its columns are adjusted. Otherwise
    CCLE is two pixels too wide, producing a scrollbar and clipping the filter.
    """
    source = (TEMPLATE_DIR / "project.html").read_text()

    assert "#myTable1_wrapper {" in source
    wrapper_rule = source[source.index("#myTable1_wrapper {"):]
    wrapper_rule = wrapper_rule[:wrapper_rule.index("}")]
    assert "min-width: 0;" in wrapper_rule

    width_reset = "$('#myTable1').css('width', '100%');"
    assert width_reset in source
    assert source.index(width_reset) < source.index("table.columns.adjust();")


def test_removing_every_sample_warns_before_it_happens():
    """Removing every sample empties the project, so it is not a silent edit.

    Jens asked for the warning on 2026-08-29, when the behaviour changed from
    "fails aggregation" to "produces an empty project": now that it succeeds,
    the user has to be told what succeeding means.

    Asserted against the template source rather than a browser, because the
    warning is client-side and browser testing is Jens's to run. What this
    pins is that the pieces exist and are wired to each other.
    """
    source = (TEMPLATE_DIR / "edit_project.html").read_text()

    # The inline banner, hidden until it applies.
    assert 'id="remove_all_warning"' in source
    assert "This removes every sample in the project." in source
    # It must say what survives, not only what is lost -- the project is kept.
    assert "you can upload samples to it again later" in source

    # One predicate, used by both the banner and the submit gate, so the two
    # cannot disagree about what "every sample" means.
    assert "function removingEverySample()" in source
    assert "boxes.length > 0 && boxes.every(cb => cb.checked)" in source

    # Shown as soon as the selection covers everything...
    assert "function syncRemoveAllWarning()" in source
    assert "syncRemoveAllWarning();" in source
    # ...and confirmed again at submit, but never on a submission that another
    # check has already rejected.
    assert "if (!e.defaultPrevented && removingEverySample())" in source
    assert "Remove all samples?" in source


def test_the_remove_all_warning_is_resynced_when_the_boxes_are_cleared():
    """Choosing replace/re-aggregate unchecks every box.

    That branch cannot call updateInputs() -- it would re-enable the checkboxes
    it just disabled -- so it has to resync the warning on its own, or a warning
    raised by an earlier selection is left on screen describing nothing.
    """
    source = (TEMPLATE_DIR / "edit_project.html").read_text()

    branch = source.split(
        "setCheckboxesDisabled(true, 'Not available when replacing or "
        "re-aggregating the project.');")[1].split('} else {')[0]
    assert 'syncRemoveAllWarning();' in branch
