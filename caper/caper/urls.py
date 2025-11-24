from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView
from django.views.i18n import set_language
import mezzanine
from mezzanine.conf import settings
from . import views
from django.conf.urls.static import static
from django.shortcuts import render, redirect


from rest_framework import routers

# Uncomment to use blog as home page. See also urlpatterns section below.
# from mezzanine.blog import views as blog_views

admin.autodiscover()


# Add the urlpatterns for any custom Django applications here.
# You can also change the ``home`` view to add your own functionality
# to the project's homepage.

urlpatterns = i18n_patterns(
    # Change the admin prefix here to use an alternate URL for the
    # admin interface, which would be marginally more secure.
    
    path("admin/", include(admin.site.urls)),
)


if settings.USE_MODELTRANSLATION:
    urlpatterns += [
        path("i18n", set_language, name="set_language"),
    ]

urlpatterns += [
    # path('single-run/', views.single_run, name='single-run'),
    path('', views.index, name='index'),
    # path('runs/', views.run_list, name='run_list'),
    path('coamplification-graph/visualizer/<gene_name>/', views.fetch_graph, name='fetch_graph'),
    path('coamplification-graph/', views.coamplification_graph, name='coamplification_graph'),
    path('coamplification-graph/visualizer/', views.visualizer, name='visualizer'),
    path('coamplification-graph/download-edges/', views.download_coamp_edges, name='download_coamp_edges'),
    path('create-project/', views.create_project, name='create_project'),
    path('create-empty-project/', views.create_empty_project, name='create_empty_project'),

    path('accounts/signup/', views.signup, name='account_signup'),
    path('accounts/', include('allauth.urls')),
    path("accounts/profile/", views.profile, name="profile"),
    path("profile-update-notification-preferences/", views.update_notification_preferences , name="profile-update-notification-preferences"),
    path("accounts/login/", views.login, name="login"),
    path("project/<project_name>", views.project_page, name="project_page"),

    path("project/<project_name>/download", views.project_download, name="project_download"),
    path("project/<project_name>/delete", views.project_delete, name="project_delete"),
    path("project/<project_name>/regenerate_project_key", views.regenerate_project_key, name="regenerate_project_key"),
    path("project/<project_name>/toggle_subscription", views.toggle_project_subscription, name="toggle_project_subscription"),

    path("project/<project_name>/sample/<sample_name>", views.sample_page, name="sample_page"),
    path("project/<project_name>/sample/<sample_name>/download", views.sample_download, name="sample_download"),
    path("project/<project_name>/sample/<sample_name>/download_metadata", views.sample_metadata_download, name="sample_metadata_download"),
    path("project/<project_name>/sample/<sample_name>/feature/<feature_name>", views.feature_page, name="feature_page"),
    path("project/<project_name>/sample/<sample_name>/feature/<feature_name>/download/<feature_id>", views.feature_download, name="feature_download"),
    path("project/<project_name>/sample/<sample_name>/feature/<feature_name>/download/png/<feature_id>", views.png_download, name="pdf_download"),
    path("project/<project_name>/sample/<sample_name>/feature/<feature_name>/download/pdf/<feature_id>", views.pdf_download, name="pdf_download"),
    path("project/<project_name>/edit", views.edit_project_page, name="edit_project_page"),
    path('gene-search/', views.gene_search_page, name='gene_search_page'),
    path("batch-sample-download/", views.batch_sample_download, name="batch_sample_download"),
    # path('gene-search/download', views.gene_search_download, name='gene_search_download'),
    # path('class-search/', views.class_search_page, name='class_search_page'),
    path('admin-featured-projects/', views.admin_featured_projects, name='admin_featured_projects'),
    path('admin-stats/', views.admin_stats, name='admin_stats'),
    path('admin-stats/download/user/',views.user_stats_download,name="user_stats_download"),
    path('admin-stats/download/project/',views.project_stats_download,name="project_stats_download"),
    path('admin-stats/site_statistics/regenerate/', views.site_stats_regenerate, name="site_stats_regenerate"),
    path('admin-sendemail/', views.admin_sendemail, name='admin_sendemail'),
    path('project/<str:project_id>/add_metadata', views.add_metadata, name='add_metadata'),
    path('project/<str:project_id>/process_metadata', views.process_metadata, name='process_metadata'),
    path('admin-version-details/', views.admin_version_details, name='admin_version_details'),
    path('admin-delete-project/', views.admin_delete_project, name='admin_delete_project'),
    path('admin-delete-user/', views.admin_delete_user, name='admin_delete_user'),
    path('admin-prepare-shutdown/', views.admin_prepare_shutdown, name='admin_prepare_shutdown'),

    path('data-qc/', views.data_qc, name='data_qc'),
    path('data-qc/change-database-dates', views.change_database_dates, name='change_database_dates'),
    path('data-qc/update_sample_counts', views.update_sample_counts, name='update_sample_counts'),
    path('data-qc/fix-schema', views.fix_schema, name='fix_schema'),
    path('upload_api/', views.FileUploadView.as_view(), name = 'Document'),
    path('add_samples_to_project_api/', views.ProjectFileAddView.as_view(), name='addSamplesToProject'),

    path('robots.txt', views.robots, name = "robots.txt"),
    path('loading/', views.loading),
    path('search_results/', views.search_results, name='search_results'),
    path('ec3d/<str:sample_name>/', views.ec3d_visualization, name='ec3d_visualization'),
    
]

urlpatterns += static(settings.PROJECT_DATA_URL, document_root=settings.PROJECT_DATA_ROOT)

urlpatterns += [
    #path(r'^accounts/', include('allauth.urls')),

    # We don't want to presume how your homepage works, so here are a
    # few patterns you can use to set it up.
    # HOMEPAGE AS STATIC TEMPLATE
    # ---------------------------
    # This pattern simply loads the index.html template. It isn't
    # commented out like the others, so it's the default. You only need
    # one homepage pattern, so if you use a different one, comment this
    # one out.
    # path("", TemplateView.as_view(template_name="index.html"), name="home"),
    # HOMEPAGE AS AN EDITABLE PAGE IN THE PAGE TREE
    # ---------------------------------------------
    # This pattern gives us a normal ``Page`` object, so that your
    # homepage can be managed via the page tree in the admin. If you
    # use this pattern, you'll need to create a page in the page tree,
    # and specify its URL (in the Meta Data section) as "/", which
    # is the value used below in the ``{"slug": "/"}`` part.
    # Also note that the normal rule of adding a custom
    # template per page with the template name using the page's slug
    # doesn't apply here, since we can't have a template called
    # "/.html" - so for this case, the template "pages/index.html"
    # should be used if you want to customize the homepage's template.
    # NOTE: Don't forget to import the view function too!
    path("", mezzanine.pages.views.page, {"slug": "/"}, name="home"),
    # HOMEPAGE FOR A BLOG-ONLY SITE
    # -----------------------------
    # This pattern points the homepage to the blog post listing page,
    # and is useful for sites that are primarily blogs. If you use this
    # pattern, you'll also need to set BLOG_SLUG = "" in your
    # ``settings.py`` module, and delete the blog page object from the
    # page tree in the admin if it was installed.
    # NOTE: Don't forget to import the view function too!
    # path("", blog_views.blog_post_list, name="home"),
    # MEZZANINE'S URLS
    # ----------------
    # ADD YOUR OWN URLPATTERNS *ABOVE* THE LINE BELOW.
    # ``mezzanine.urls`` INCLUDES A *CATCH ALL* PATTERN
    # FOR PAGES, SO URLPATTERNS ADDED BELOW ``mezzanine.urls``
    # WILL NEVER BE MATCHED!
    # If you'd like more granular control over the patterns in
    # ``mezzanine.urls``, go right ahead and take the parts you want
    # from it, and use them directly below instead of using
    # ``mezzanine.urls``.
    path("", include("mezzanine.urls")),
    # MOUNTING MEZZANINE UNDER A PREFIX
    # ---------------------------------
    # You can also mount all of Mezzanine's urlpatterns under a
    # URL prefix if desired. When doing this, you need to define the
    # ``SITE_PREFIX`` setting, which will contain the prefix. Eg:
    # SITE_PREFIX = "my/site/prefix"
    # For convenience, and to avoid repeating the prefix, use the
    # commented out pattern below (commenting out the one above of course)
    # which will make use of the ``SITE_PREFIX`` setting. Make sure to
    # add the import ``from django.conf import settings`` to the top
    # of this file as well.
    # Note that for any of the various homepage patterns above, you'll
    # need to use the ``SITE_PREFIX`` setting as well.
    # ("^%s/" % settings.SITE_PREFIX, include("mezzanine.urls"))
]



# Adds ``STATIC_URL`` to the context of error pages, so that error
# pages can use JS, CSS and images.
handler404 = "mezzanine.core.views.page_not_found"
handler500 = "mezzanine.core.views.server_error"
