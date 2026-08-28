"""
How the 'private' field is encoded, in one place.

The field is named like a boolean and is not one.  It holds one of three
visibility strings; documents written before that change hold a boolean
instead, and both encodings are live on production (measured 2026-08-27:
'private' 202, 'public' 93, 'hidden_public' 15, boolean True 1, of 311
documents).

This module has no Django imports on purpose, so the standalone operational
scripts can share it rather than re-deriving the mapping.  ``utils`` re-exports
everything here, so existing ``from .utils import ...`` call sites are
unaffected.

tests/test_visibility_encoding_guard.py holds the rest of the codebase to
going through these helpers.
"""

# The canonical visibility strings, in the order the form presents them.  This
# is the same set schema.json declares as the enum for 'private'; the guard test
# checks the two agree.
VISIBILITY_VALUES = ('private', 'public', 'hidden_public')

# What to match a Mongo query against, per visibility.  Documents written before
# the string visibilities hold a boolean, so both encodings are listed -- and
# listed *once*, because these lists were hand-copied into ten query sites
# across six modules, which is how one of them ends up a value behind.
#
# The three lists are mutually exclusive and cover every value measured on
# production 2026-08-27 ('private' 202, 'public' 93, 'hidden_public' 15,
# boolean True 1; no boolean False was found, but it is matched anyway because
# normalize_visibility_field maps it to 'public').
VISIBILITY_QUERY_VALUES = {
    'public': [False, 'public'],
    'private': [True, 'private'],
    'hidden_public': ['hidden_public'],
}

# Everything that is not publicly listed: 'private' and 'hidden_public'
# together.  This is the access-control sense, and it is deliberately *not* the
# same list as VISIBILITY_QUERY_VALUES['private'] -- site statistics count
# hidden_public in its own bucket while access control folds it in with private
# (see is_project_private).  Conflating them is the reason both exist by name.
PUBLIC_QUERY_VALUES = VISIBILITY_QUERY_VALUES['public']
RESTRICTED_QUERY_VALUES = (
    VISIBILITY_QUERY_VALUES['private'] + VISIBILITY_QUERY_VALUES['hidden_public']
)


def normalize_visibility_field(private_value):
    """
    Normalize legacy boolean private field to new string visibility format.
    
    For backward compatibility with API calls that use boolean values:
    - True -> 'private'
    - False -> 'public'
    - String values are returned as-is
    
    Args:
        private_value: Boolean (True/False) or string ('private', 'public', 'hidden_public')
    
    Returns:
        String visibility value ('private', 'public', or 'hidden_public')
    """
    if isinstance(private_value, bool):
        return 'private' if private_value else 'public'
    elif isinstance(private_value, str):
        if private_value in VISIBILITY_VALUES:
            return private_value
        # Handle string representations of booleans (from URL params, etc.)
        if private_value.lower() in ('true', '1', 'yes'):
            return 'private'
        elif private_value.lower() in ('false', '0', 'no'):
            return 'public'
    return 'private'  # Default to private for safety


def is_project_private(visibility):
    """
    Check if a project should be treated as private.
    
    Returns True for 'private' and 'hidden_public' (hidden_public is private
    in terms of statistics and access control, just visible to anyone with the link).
    
    Args:
        visibility: String visibility value ('private', 'public', or 'hidden_public')
    
    Returns:
        Boolean indicating if project is private
    """
    return visibility in ('private', 'hidden_public')


def is_project_public(visibility):
    """
    Check if a project is fully public.
    
    Returns True only for 'public'.
    
    Args:
        visibility: String visibility value ('private', 'public', or 'hidden_public')
    
    Returns:
        Boolean indicating if project is public
    """
    return visibility == 'public'


def is_project_hidden_public(visibility):
    """
    Check if a project is hidden_public.
    
    Args:
        visibility: String visibility value ('private', 'public', or 'hidden_public')
    
    Returns:
        Boolean indicating if project is hidden_public
    """
    return visibility == 'hidden_public'


def format_visibility_for_display(private_value):
    """
    Format visibility value for display to users.
    
    Converts both legacy boolean values and new string values to
    user-friendly display strings.
    
    Args:
        private_value: Boolean (True/False) or string ('private', 'public', 'hidden_public')
    
    Returns:
        Display string: 'Private', 'Public', or 'Hidden Public'
    """
    # First normalize the value
    normalized = normalize_visibility_field(private_value)
    
    # Convert to display format
    if normalized == 'private':
        return 'Private'
    elif normalized == 'public':
        return 'Public'
    elif normalized == 'hidden_public':
        return 'Hidden Public'
    else:
        return 'Private'  # Default fallback
