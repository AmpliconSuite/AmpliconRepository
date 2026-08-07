"""
Safe extraction of untrusted tar archives.

Project tarballs are attacker-controlled — ``FileUploadView``
(``POST /upload_api/``) accepts uploads with no authentication — and
``TarFile.extractall()`` / ``TarFile.extract()`` do not defend the destination
directory on their own.  Without an explicit ``filter=`` argument the default
is ``fully_trusted`` on every Python this project runs on (3.10 in the Docker
image, 3.12 locally); the safe default only arrives in Python 3.14.  A member
named ``results/../../../../x`` is written wherever the ``..`` sequence
resolves to, which for the media directory means the application source tree.

Every extraction of an uploaded or stored tarball must therefore go through
``safe_extractall()`` or ``safe_extract_member()`` below.  Both apply the
stdlib ``data`` filter, which rejects members resolving outside the
destination, absolute paths, and links pointing outside the archive, while
permitting ordinary files and directories.

Rejected members are **skipped and logged**, not raised: a single bad member in
an otherwise legitimate upload must not cost the user the rest of their data,
and it must not disappear silently either — a rejection in the log is how a
genuine archive that trips the filter gets noticed.
"""

import logging
import os
import tarfile

logger = logging.getLogger(__name__)

# ``tarfile.data_filter`` landed in 3.12 and was backported to 3.8.17, 3.9.17,
# 3.10.12 and 3.11.4, together with the ``filter=`` argument to extract() and
# extractall().  Older interpreters fall back to the equivalent checks below
# rather than silently extracting without protection.
_HAS_DATA_FILTER = hasattr(tarfile, 'data_filter')


class UnsafeTarMember(Exception):
    """A tar member that would write outside its destination directory."""


def _fallback_check(member, dest_path):
    """Stand-in for ``tarfile.data_filter`` on pre-backport interpreters.

    Returns *member* if it is safe to extract into *dest_path*, otherwise
    raises :class:`UnsafeTarMember`.
    """
    name = member.name

    if os.path.isabs(name) or os.path.splitdrive(name)[0]:
        raise UnsafeTarMember(f'{name!r} is an absolute path')

    # realpath rather than abspath so that a symlink planted earlier in the
    # same archive cannot be used as a stepping stone out of the destination.
    dest = os.path.realpath(dest_path)
    target = os.path.realpath(os.path.join(dest, name))
    if target != dest and not target.startswith(dest + os.sep):
        raise UnsafeTarMember(
            f'{name!r} would be extracted to {target!r}, which is outside the '
            f'destination {dest!r}')

    if member.issym() or member.islnk():
        linkname = member.linkname
        if os.path.isabs(linkname):
            raise UnsafeTarMember(f'{name!r} links to absolute path {linkname!r}')
        # A hardlink name is relative to the archive root; a symlink target is
        # relative to the directory holding the link.
        base = dest if member.islnk() else os.path.dirname(target)
        link_target = os.path.realpath(os.path.join(base, linkname))
        if link_target != dest and not link_target.startswith(dest + os.sep):
            raise UnsafeTarMember(
                f'{name!r} links to {linkname!r}, which is outside the '
                f'destination {dest!r}')

    if member.isdev():
        raise UnsafeTarMember(f'{name!r} is a device or special file')

    return member


def _check_member(member, dest_path):
    """Return a sanitised *member*, or raise :class:`UnsafeTarMember`."""
    if not _HAS_DATA_FILTER:
        return _fallback_check(member, dest_path)
    try:
        return tarfile.data_filter(member, dest_path)
    except tarfile.FilterError as err:
        raise UnsafeTarMember(str(err)) from err


def safe_extractall(tar, path, members=None, description=None):
    """Extract *tar* into *path*, skipping and logging unsafe members.

    Drop-in replacement for ``tar.extractall(path=path, members=members)``.

    Args:
        tar: an open ``tarfile.TarFile``
        path: destination directory
        members: optional subset of members to extract (defaults to all)
        description: optional label for the archive, used in log messages

    Returns:
        list: names of the members that were refused, in archive order.
    """
    label = f' ({description})' if description else ''
    rejected = []

    def _filter(member, dest_path):
        try:
            return _check_member(member, dest_path)
        except UnsafeTarMember as err:
            rejected.append(member.name)
            logger.warning('Refusing unsafe tar member%s: %s', label, err)
            return None

    if _HAS_DATA_FILTER:
        # The filter runs per member inside extractall, so a rejection skips
        # only that member instead of aborting the whole archive.
        tar.extractall(path=path, members=members, filter=_filter)
    else:
        if members is None:
            members = tar.getmembers()
        safe = [checked for checked in
                (_filter(member, path) for member in members)
                if checked is not None]
        tar.extractall(path=path, members=safe)

    if rejected:
        logger.error(
            'Refused %d unsafe member(s)%s while extracting to %s: %s',
            len(rejected), label, path, ', '.join(rejected[:10]))

    return rejected


def safe_extract_member(tar, member, path, description=None):
    """Extract a single member of *tar* into *path* unless it is unsafe.

    Drop-in replacement for ``tar.extract(member, path=path)``.

    Args:
        tar: an open ``tarfile.TarFile``
        member: a ``TarInfo``, or a member name to look up
        path: destination directory
        description: optional label for the archive, used in log messages

    Returns:
        bool: True if the member was extracted, False if it was refused.

    Raises:
        KeyError: if *member* is a name that is not in the archive — matching
            ``TarFile.extract()``, which callers rely on to fall back to an
            alternative member name.
    """
    if isinstance(member, str):
        member = tar.getmember(member)
    return not safe_extractall(tar, path, members=[member],
                               description=description)
