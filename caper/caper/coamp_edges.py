"""The co-amplification edge CSV, written when the graph is built.

``download_coamp_edges`` rebuilt the whole graph -- every selected project
read in full, the Graph constructed, the CSV exported -- and then discarded
it.  The one real download in prod's access log since 2026-08-30 took 38.9 s;
for the two-project graphs in prod's log the same rebuild is 200-300 s of one
worker and a ~2 GiB peak, for a file that was fully determined the moment the
graph was built.

So the CSV is written here, beside the neo4j cache it belongs to, at the point
the Graph object exists.  Both variants are written -- with and without the
per-edge sample-id columns -- from one export, since the download offers
either and re-reading a 750k-row CSV to drop three columns costs more than
writing it twice.

The files live on the box, keyed by the same ``cache_key`` as the neo4j graph,
and are removed by the same ``_clear_cache_keys`` that removes the graph, so
they can never outlive it.  They can be missing while the graph exists -- a
graph built before this module, or a container rebuilt on a directory that was
not mounted -- and the download then falls back to the rebuild, once, and
writes the files it should have found.
"""

import gzip
import logging
import os
import shutil
import tempfile

SAMPLE_ID_COLUMNS = ['gene1_sample_ids', 'gene2_sample_ids', 'shared_sample_ids']


def edges_dir():
    """Where the files go.  Under CAPER_ROOT, which the containers bind-mount
    from the host, so a container restart or rebuild does not lose them."""
    configured = os.environ.get('COAMP_EDGES_DIR')
    if configured:
        return configured
    return os.path.join(os.environ.get('CAPER_ROOT', '.'), 'coamp_edges')


def edges_path(cache_key, include_sample_ids):
    suffix = 'with_samples' if include_sample_ids else 'edges'
    return os.path.join(edges_dir(), f'{cache_key}.{suffix}.csv.gz')


def save_edges(cache_key, graph):
    """Write both CSV variants for ``graph``.  Returns the edge count, or None
    if the graph has no edges (nothing is written then, so the download's
    "No edges found" path still runs)."""
    edges_df = graph.get_edges_dataframe(include_sample_ids=True)
    if edges_df.empty:
        return None
    directory = edges_dir()
    os.makedirs(directory, exist_ok=True)
    for include_sample_ids in (True, False):
        frame = edges_df if include_sample_ids else edges_df.drop(columns=SAMPLE_ID_COLUMNS)
        target = edges_path(cache_key, include_sample_ids)
        # Write-then-rename so a reader never sees a half-written file.
        fd, partial = tempfile.mkstemp(dir=directory, prefix='.partial-', suffix='.csv.gz')
        os.close(fd)
        try:
            with gzip.open(partial, 'wt', newline='') as fh:
                frame.to_csv(fh, index=False)
            os.replace(partial, target)
        except BaseException:
            try:
                os.remove(partial)
            except OSError:
                pass
            raise
    logging.info("[PERF] co-amplification edges saved for %s: %d edges", cache_key, len(edges_df))
    return len(edges_df)


def open_edges(cache_key, include_sample_ids):
    """A binary file object yielding the plain CSV, or None if not saved."""
    path = edges_path(cache_key, include_sample_ids)
    try:
        return gzip.open(path, 'rb')
    except FileNotFoundError:
        return None


def remove_edges(cache_key):
    """Drop both variants.  Silent if absent: the graph is being cleared and
    the files may never have been written for it."""
    for include_sample_ids in (True, False):
        try:
            os.remove(edges_path(cache_key, include_sample_ids))
        except FileNotFoundError:
            pass


def remove_all_edges():
    shutil.rmtree(edges_dir(), ignore_errors=True)
