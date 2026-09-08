"""
Schema-generation support for the OpenAPI document at /api/v1/openapi.json.

Two things live here: the preprocessing hook that limits the document to the
public read API, and the reusable response components the view annotations
refer to.
"""

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, inline_serializer
from rest_framework import serializers

from .api_errors import API_V1_PREFIX


def only_api_v1(endpoints, **kwargs):
    """
    drf-spectacular PREPROCESSING_HOOKS: keep only `/api/v1/` paths.

    Django's URLconf also carries `/upload_api/` and
    `/add_samples_to_project_api/`.  Those are DRF views, so the generator finds
    them, but they are write endpoints whose contract belongs to
    AmpliconSuiteAggregator rather than to this document.  Publishing them here
    would advertise a mutation surface the public API deliberately does not
    have -- the API is documented as read-only, and the document must not
    contradict that.
    """
    return [(path, path_regex, method, callback)
            for path, path_regex, method, callback in endpoints
            if path.startswith(API_V1_PREFIX)]


# ── Reusable error component ────────────────────────────────────────────────

ErrorSerializer = inline_serializer(
    name='Error',
    fields={
        'error': serializers.CharField(
            help_text='Human-readable message. Not a contract; do not parse.'),
        'code': serializers.CharField(
            help_text='Stable machine-readable slug. Branch on this.'),
        'retry_after': serializers.IntegerField(
            required=False,
            help_text='Seconds to wait before retrying. Present on 429.'),
    },
)


def _err(description, code, message, status_code=None):
    return OpenApiResponse(
        response=ErrorSerializer,
        description=description,
        examples=[OpenApiExample('example', value={'error': message, 'code': code})],
    )


NOT_FOUND = _err(
    'No project resolves to this id, or it has been deleted.',
    'not_found', 'Project not found')

AUTH_REQUIRED = _err(
    'The project is private and the caller is anonymous. Retry with a token.',
    'authentication_required', 'Authentication required')

FORBIDDEN = _err(
    'The caller is authenticated but is not a member of this private project. '
    'Retrying with the same token will not help.',
    'permission_denied', 'You do not have access to this project')

RATE_LIMITED = OpenApiResponse(
    response=ErrorSerializer,
    description='Rate limit exceeded. Wait `retry_after` seconds and retry.',
    examples=[OpenApiExample('example', value={
        'error': 'Request was throttled. Expected available in 30 seconds.',
        'code': 'rate_limited', 'retry_after': 30})],
)

NO_ARCHIVE = _err(
    'The project exists and is readable, but has no downloadable archive.',
    'no_archive', 'No archive available for this project')

DOWNLOAD_UNAVAILABLE = _err(
    'The archive exists but could not be served right now. Transient; retry.',
    'service_unavailable', 'Download temporarily unavailable')


# ── Response components ─────────────────────────────────────────────────────
#
# These describe what the views already return; they are documentation, not
# validation, and no view runs data through them.  Keep them in step with
# _project_to_dict() and _sample_to_dict() in views_apis.py.

class PreviousVersionSerializer(serializers.Serializer):
    date = serializers.CharField()
    linkid = serializers.CharField(help_text='Use as the `id` path parameter to fetch this version.')
    AA_version = serializers.CharField(required=False)
    AC_version = serializers.CharField(required=False)
    ASP_version = serializers.CharField(required=False)
    aggregator_version = serializers.CharField(required=False)
    Reconstruction_tools = serializers.CharField(required=False)
    CoRAL_version = serializers.CharField(required=False)


class ProjectSerializer(serializers.Serializer):
    """One project's metadata. Sample-level data is at `.../samples/`."""

    id = serializers.CharField(
        help_text='Stable project identifier. Use it in every other path.')
    project_name = serializers.CharField()
    description = serializers.CharField()
    sample_count = serializers.IntegerField()
    visibility = serializers.ChoiceField(
        choices=['public', 'private', 'hidden_public'],
        help_text='`public` is listed and world-readable; `hidden_public` is '
                  'readable by link but unlisted; `private` needs membership.')
    date = serializers.CharField(help_text='ISO-8601 timestamp.')
    publication_link = serializers.CharField(allow_blank=True)
    creator = serializers.CharField()
    reference_genome = serializers.CharField(
        allow_blank=True, help_text='e.g. GRCh38, hg19.')
    AA_version = serializers.CharField(allow_blank=True)
    AC_version = serializers.CharField(allow_blank=True)
    ASP_version = serializers.CharField(allow_blank=True)
    aggregator_version = serializers.CharField(allow_blank=True)
    reconstruction_tools = serializers.CharField(allow_blank=True)
    CoRAL_version = serializers.CharField(allow_blank=True)
    oncogenes = serializers.ListField(
        child=serializers.CharField(),
        help_text='Gene symbols amplified somewhere in this project.')
    classifications = serializers.ListField(
        child=serializers.CharField(),
        help_text='Amplicon classes present anywhere in this project, in the '
                  'same spelling the sample rows use: ecDNA, BFB, Linear, '
                  'Complex-non-cyclic, FAN. Filter on this to find projects '
                  'with ecDNA.')
    version = serializers.IntegerField(
        help_text='Which version of this project the response describes, '
                  'counting from 1. Resolving a project by name always returns '
                  'its current version; cite this number alongside the id when '
                  'reporting results.')
    version_count = serializers.IntegerField(
        help_text='How many versions this project has in total.')
    is_latest_version = serializers.BooleanField(
        help_text='False when this id names a superseded version, which still '
                  'resolves so that published results stay reachable.')
    previous_versions = PreviousVersionSerializer(
        many=True,
        help_text='Superseded versions, oldest first. Each still resolves.')


class SampleSerializer(serializers.Serializer):
    """
    One sample row.

    Only `run` is guaranteed. The remaining keys come from the uploaded
    aggregator output and vary with the pipeline version that produced the
    project, so a client must tolerate unknown keys and absent ones. Fields
    holding server-side file references are removed before serialization.
    """

    run = serializers.CharField(help_text='Run this sample belongs to.')
    Sample_name = serializers.CharField(required=False)


class BatchDownloadRequestSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.CharField(),
        help_text='Project ids to resolve. Required; an absent or misspelled '
                  'key is a 400, not an empty result.')


class BatchDownloadItemSerializer(serializers.Serializer):
    id = serializers.CharField()
    project_name = serializers.CharField()
    download_url = serializers.URLField(
        help_text='Absolute URL of this project\'s download endpoint.')


class BatchDownloadResponseSerializer(serializers.Serializer):
    downloads = BatchDownloadItemSerializer(many=True)
    skipped = serializers.ListField(
        child=serializers.CharField(),
        help_text='Ids that resolved to nothing downloadable: unknown, not '
                  'readable by this caller, or with no archive. The reasons '
                  'are not distinguished; fetch the project individually to '
                  'find out which applies.')


BAD_REQUEST = _err(
    'The request body was missing, malformed, or the wrong shape.',
    'ids_required',
    "Request body must be a JSON object with an 'ids' key")


class TokenStatusSerializer(serializers.Serializer):
    has_token = serializers.BooleanField()
    token_suffix = serializers.CharField(
        allow_null=True,
        help_text='Last 8 characters, for confirming which token is stored.')


class TokenSerializer(serializers.Serializer):
    token = serializers.CharField(
        help_text='The full token. Shown once, at creation. Store it securely.')


class TokenRevokedSerializer(serializers.Serializer):
    detail = serializers.CharField()


LOGIN_REQUIRED = _err(
    'No logged-in browser session. An API token cannot authenticate here.',
    'login_required', 'Login required')

NO_TOKEN = _err('The caller has no token to revoke.', 'no_token',
                'No active token to revoke')


INDEX_UNAVAILABLE = _err(
    'The search index is rebuilding and cannot answer accurately yet.',
    'index_unavailable',
    'The search index is rebuilding and cannot answer accurately yet. Retry shortly.',
    status_code=503)


class FeatureRowSerializer(serializers.Serializer):
    """One focal amplification, as a search result.

    `genes` and `oncogenes` are reported in the spelling the source data uses,
    not the upper-cased form the index matches on -- 1,083 symbols in the corpus
    are not upper-case, and reporting C17ORF37 for C17orf37 would be reporting a
    name that does not exist.
    """
    project_id = serializers.CharField()
    project_name = serializers.CharField()
    sample_name = serializers.CharField()
    feature_id = serializers.CharField()
    classification = serializers.CharField(
        help_text='Amplicon class in the same spelling the sample rows use: '
                  'ecDNA, BFB, Linear, Complex-non-cyclic, FAN, Virus.')
    genes = serializers.ListField(child=serializers.CharField())
    oncogenes = serializers.ListField(child=serializers.CharField())
    locations = serializers.ListField(child=serializers.CharField())
    reference_build = serializers.CharField()
    sample_type = serializers.CharField(allow_null=True)
    cancer_type = serializers.CharField(allow_null=True)
    tissue_of_origin = serializers.CharField(allow_null=True)
    project_url = serializers.CharField()
    sample_url = serializers.CharField(
        help_text='Fetch this to get the sample rows behind the match. Search '
                  'returns rows, never payload; every row carries the URL to '
                  'fetch its data.')


class FeatureSearchSerializer(serializers.Serializer):
    count = serializers.IntegerField(
        help_text='Total matching rows, not the number on this page.')
    results = FeatureRowSerializer(many=True)
    next_cursor = serializers.CharField(
        allow_null=True,
        help_text='Pass as ?cursor= for the next page. Null on the last page.')
    reference_builds = serializers.DictField(
        child=serializers.IntegerField(),
        help_text='How the whole result set splits by reference build. Gene '
                  'symbols are build-dependent, so a zero on one build is the '
                  'signal that the gene may exist there under another name.')


class FacetValueSerializer(serializers.Serializer):
    value = serializers.CharField()
    count = serializers.IntegerField()


class FacetSetSerializer(serializers.Serializer):
    classification = FacetValueSerializer(many=True)
    sample_type = FacetValueSerializer(many=True)
    cancer_type = FacetValueSerializer(many=True)
    tissue_of_origin = FacetValueSerializer(many=True)
    reference_build = FacetValueSerializer(many=True)


class FeatureFacetsSerializer(serializers.Serializer):
    """Facet values, plus the total they should be read against.

    Each key under ``facets`` is also the query parameter that filters on it.
    A facet's counts summing to less than ``total_rows`` means the remaining
    rows carry no value for that field at all, so a filter on it can never
    reach them -- the filtered count is a floor, not an answer.
    """
    total_rows = serializers.IntegerField()
    facets = FacetSetSerializer()
