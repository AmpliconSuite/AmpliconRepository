def test_admin_stats_project_rows_include_modern_public_and_private_projects():
    from caper.views_admin import _partition_admin_stats_projects

    projects = [
        {'project_name': 'modern-public', 'private': 'public'},
        {'project_name': 'legacy-public', 'private': False},
        {'project_name': 'modern-private', 'private': 'private'},
        {'project_name': 'hidden', 'private': 'hidden_public'},
    ]

    public, private = _partition_admin_stats_projects(projects)

    assert [p['project_name'] for p in public] == [
        'modern-public', 'legacy-public'
    ]
    assert [p['project_name'] for p in private] == [
        'modern-private', 'hidden'
    ]


def test_admin_stats_template_lists_private_projects_without_links():
    from pathlib import Path

    template = (Path(__file__).parents[1] / 'caper' / 'templates' / 'pages' /
                'admin_stats.html').read_text()

    assert '{% for project in private_projects %}' in template
    private_section = template.split(
        '{% for project in private_projects %}', 1
    )[1].split('{% endfor %}', 1)[0]
    assert 'project.project_name' in private_section
    assert "url 'project_page'" not in private_section
