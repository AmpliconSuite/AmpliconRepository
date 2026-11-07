"""Reading a site archive, whatever aggregator built it, and staging its samples for AC.

Archives differ by the aggregator version that built them: older ones have
`results/AA_outputs/<project>/` and `other_files/classification/`, newer ones
`results/samples/<sample>/` and `consolidated_classification/`. Discovery works from
`run.json` where it can and from file names where it can't.
"""

import hashlib
import json
import os
import re
import shutil
import tarfile
from collections import defaultdict

from .plan import REF_MAP


class ProjectError(Exception):
    """This project can't continue. Other projects in the batch still can."""


def extract(tar_path, dest):
    """Unpack untrusted archive contents. The filter refuses paths that leave dest."""
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest, filter='data')


def clean_mac_junk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.startswith('._') or name == '.DS_Store':
                os.remove(os.path.join(dirpath, name))
        if '__MACOSX' in dirnames:
            shutil.rmtree(os.path.join(dirpath, '__MACOSX'), ignore_errors=True)
            dirnames.remove('__MACOSX')


def find_files(root, suffix):
    found = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(suffix) and not name.startswith('.'):
                found.append(os.path.join(dirpath, name))
    return found


def results_root(unpacked):
    results = os.path.join(unpacked, 'results')
    return results if os.path.isdir(results) else unpacked


# ---- discovery ----

def _resolve(base, relpath):
    """A path from run.json, if it exists and stays inside the archive."""
    if not relpath or relpath in ('Not Provided', 'None'):
        return None
    path = os.path.realpath(os.path.join(base, relpath))
    if not path.startswith(os.path.realpath(base) + os.sep):
        return None
    return path if os.path.exists(path) else None


def discover_from_runjson(root):
    """Each sample's paths, from run.json. It handles names that file patterns can't, such as
    a sample `PC3_DM5` whose CNV file is `DM5hg38_CNV_CALLS.bed`. {} if there's no usable one."""
    manifest = next((os.path.join(dirpath, 'run.json') for dirpath, _, names in os.walk(root)
                     if 'run.json' in names), None)
    if not manifest:
        return {}
    base = os.path.dirname(manifest)
    try:
        with open(manifest) as handle:
            data = json.load(handle)
    except ValueError:
        return {}
    samples = {}
    for features in data.get('runs', {}).values():
        for feature in features:
            name = feature.get('Sample name')
            if not name or name in samples:
                continue
            paths = {}
            # Aggregator 7.0 renamed this field. Missing it sends every sample to the
            # recovery path, which drops the AA log, and then the site can't share directories.
            aa = _resolve(base, feature.get('Reconstruction directory') or feature.get('AA directory'))
            if aa and aa.endswith('.tar.gz'):
                paths['aa_tarball'] = aa
            elif aa and os.path.isdir(aa):
                paths['aa_dir'] = aa
            # Kept even when the AA output wasn't packed: a sample can have a valid CNV BED
            # while its reconstruction lives only in AC's files/ copy.
            for key, field in (('cnv_calls_bed', 'CNV BED file'),
                               ('sample_meta', 'Sample metadata JSON'),
                               ('run_meta', 'Run metadata JSON')):
                path = _resolve(base, feature.get(field))
                if path:
                    paths[key] = path
            if paths:
                samples[name] = paths
    return samples


def _ac_files_dirs(root):
    """Directories named `files`: AC's copy of graph, cycles and summary."""
    return [dirpath for dirpath, _, names in os.walk(root)
            if os.path.basename(dirpath) == 'files' and any(n.endswith('_graph.txt') for n in names)]


def _has_aa(paths):
    return any(key in paths for key in ('aa_dir', 'aa_tarball', 'aa_files_dir'))


def _recover_unpacked_aa(root, samples):
    """Find AA data for samples with no packed AA output.

    It may be in a loose directory with every amplicon's plots, or in AC's files/ copy, which
    keeps plots only for classified amplicons. Take the one with the most plots, or the
    unclassified amplicons lose their images in the rebuild.
    """
    packed = {s for s, paths in samples.items() if 'aa_dir' in paths or 'aa_tarball' in paths}
    candidates = set(_ac_files_dirs(root))
    for dirpath, _, names in os.walk(root):
        if any(n.endswith('_summary.txt') and not n.startswith('.') for n in names):
            candidates.add(dirpath)
    listings = {d: os.listdir(d) for d in candidates}
    needed = set(samples)
    for names in listings.values():
        for name in names:
            if name.startswith('.'):
                continue
            if name.endswith('_graph.txt') and '_amplicon' in name:
                needed.add(name.split('_amplicon')[0])
            elif name.endswith('_summary.txt'):
                needed.add(name[:-len('_summary.txt')])
    for sample in needed - packed:
        best, best_score = None, (-1, -1, -1)
        for directory, names in listings.items():
            graphs = sum(n.startswith(sample + '_amplicon') and n.endswith('_graph.txt') for n in names)
            summary = (sample + '_summary.txt') in names
            if not (graphs or summary):
                continue
            plots = sum(n.startswith(sample + '_amplicon') and n.endswith('.png') for n in names)
            score = (plots, graphs, int(summary))
            if score > best_score:
                best, best_score = directory, score
        if best is not None:
            samples[sample].setdefault('aa_files_dir', best)


def _attach_named_files(root, samples):
    """Add what run.json didn't supply, matched by file name to known samples only."""
    known = set(samples)
    suffixes = (('_AA_CNV_SEEDS.bed', 'cnv_seeds_bed'), ('_sample_metadata.json', 'sample_meta'),
                ('_run_metadata.json', 'run_meta'), ('_cnvkit_output.tar.gz', 'cnvkit_tarball'))
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            if name.endswith(('_cnvkit_output', '_cnvkit_outputs')):
                sample = re.sub(r'_cnvkit_outputs?$', '', name)
                if sample in known:
                    samples[sample].setdefault('cnvkit_dir', os.path.join(dirpath, name))
        for name in filenames:
            if name.startswith('.'):
                continue
            path = os.path.join(dirpath, name)
            if name.endswith('_CNV_CALLS.bed'):
                sample = name[:-len('_CNV_CALLS.bed')].split('.')[0]
                if sample in known:
                    samples[sample].setdefault('cnv_calls_bed', path)
                continue
            for suffix, key in suffixes:
                if name.endswith(suffix) and name[:-len(suffix)] in known:
                    samples[name[:-len(suffix)]].setdefault(key, path)
    # An old single-sample layout has a bare cnvkit_output/, which is unambiguous only then.
    if len(samples) == 1:
        bare = [os.path.join(dirpath, d) for dirpath, dirnames, _ in os.walk(root)
                for d in dirnames if d in ('cnvkit_output', 'cnvkit_outputs')]
        if bare:
            samples[next(iter(samples))].setdefault('cnvkit_dir', bare[0])


def discover_samples(root):
    """{sample: paths}. Keeps samples with AA data or a CNV BED: a sample with only a CNV has
    no amplification, and it belongs in the archive as part of the denominator."""
    samples = defaultdict(dict)
    for name, paths in discover_from_runjson(root).items():
        samples[name].update(paths)
    if not any(_has_aa(paths) for paths in samples.values()):
        for dirpath, dirnames, _ in os.walk(root):
            for name in dirnames:
                if name.endswith('_AA_results'):
                    samples[name[:-len('_AA_results')]]['aa_dir'] = os.path.join(dirpath, name)
        for path in find_files(root, '_AA_results.tar.gz'):
            samples[os.path.basename(path)[:-len('_AA_results.tar.gz')]].setdefault('aa_tarball', path)
    _recover_unpacked_aa(root, samples)
    _attach_named_files(root, samples)
    return {name: paths for name, paths in samples.items()
            if _has_aa(paths) or paths.get('cnv_calls_bed')}


def old_ac_dirs(root):
    """Every directory with AC profiles. A project may have one, or one per sample."""
    return sorted({os.path.dirname(p)
                   for p in find_files(root, '_amplicon_classification_profiles.tsv')})


# ---- staging ----

def _stage_aa(sample, paths, sample_dir):
    dest = os.path.join(sample_dir, sample + '_AA_results')
    if 'aa_dir' in paths:
        shutil.copytree(paths['aa_dir'], dest)
    elif 'aa_files_dir' in paths:
        os.makedirs(dest)
        for name in os.listdir(paths['aa_files_dir']):
            if name.startswith(sample + '_amplicon') or name == sample + '_summary.txt':
                shutil.copy(os.path.join(paths['aa_files_dir'], name), os.path.join(dest, name))
    elif 'aa_tarball' in paths:
        unpacked = os.path.join(sample_dir, '_untar')
        extract(paths['aa_tarball'], unpacked)
        found = None
        for dirpath, dirnames, _ in os.walk(unpacked):
            if sample + '_AA_results' in dirnames:
                found = os.path.join(dirpath, sample + '_AA_results')
        if not found:
            found = next((dirpath for dirpath, _, names in os.walk(unpacked)
                          if any(n.endswith('_graph.txt') for n in names)), None)
        if not found:
            raise ProjectError('no AA results inside %s' % paths['aa_tarball'])
        shutil.move(found, dest)
        shutil.rmtree(unpacked, ignore_errors=True)


def stage_samples(samples, stage_dir):
    """One uniform tree per sample, which is both AC's input and the aggregator's."""
    for sample, paths in samples.items():
        if os.sep in sample or sample in ('.', '..'):
            raise ProjectError('run.json has an unusable sample name: %r' % sample)
        sample_dir = os.path.join(stage_dir, sample)
        os.makedirs(sample_dir)
        _stage_aa(sample, paths, sample_dir)
        if paths.get('cnv_calls_bed'):
            shutil.copy(paths['cnv_calls_bed'], os.path.join(sample_dir, sample + '_CNV_CALLS.bed'))
        if paths.get('cnvkit_dir'):
            shutil.copytree(paths['cnvkit_dir'], os.path.join(sample_dir, sample + '_cnvkit_output'))
        elif paths.get('cnvkit_tarball'):
            shutil.copy(paths['cnvkit_tarball'], os.path.join(sample_dir, sample + '_cnvkit_output.tar.gz'))
        for key, suffix in (('cnv_seeds_bed', '_AA_CNV_SEEDS.bed'),
                            ('sample_meta', '_sample_metadata.json'),
                            ('run_meta', '_run_metadata.json')):
            if paths.get(key):
                shutil.copy(paths[key], os.path.join(sample_dir, sample + suffix))
    clean_mac_junk(stage_dir)


# ---- how AC was run ----

AC_FLAGS_VALUED = ('--min_flow', '--min_size', '--decomposition_strictness', '--filter_pval',
                   '--exclude_bed')
AC_FLAGS_BOOL = ('--filter_similar', '--add_chr_tag')
DEFAULT_CONFIG = 'default config'


def _parse_ac_command(command):
    tokens = command.split()
    ref, flags = None, []
    for i, token in enumerate(tokens):
        if token == '--ref' and i + 1 < len(tokens):
            ref = tokens[i + 1]
        elif token in AC_FLAGS_BOOL:
            flags.append(token)
        elif token in AC_FLAGS_VALUED and i + 1 < len(tokens):
            flags += [token, tokens[i + 1]]
    return ref, flags


def recover_settings(root, api_reference):
    """How AC made these results: the AC log if present, then run metadata, then the API.

    A log wins over run metadata, because AC may have been rerun after the pipeline. Settings
    from run metadata can be older than the results beside them, and they're noted as such.

    `notes` need a person's attention. `info` doesn't: on 2026-11-06 the rerun message fired on
    both real projects tried, because AC is commonly rerun after the pipeline.
    """
    notes, info = [], []
    log_ref, log_version, log_flags = None, None, []
    variants, config_files = set(), set()
    logs = set(find_files(root, '.log')) | set(find_files(root, '_AA_AC_out.txt'))
    for path in sorted(logs):
        with open(path, errors='ignore') as handle:
            text = handle.read()
        command = next((line for line in text.splitlines()
                        if 'amplicon_classifier.py' in line and '--ref' in line
                        and 'make_input' not in line and 'make_results' not in line), None)
        if not command:
            continue
        ref, flags = _parse_ac_command(command)
        if not ref:
            continue
        version = re.search(r'AmpliconClassifier\s+v?(\d+\.\d+\.\d+)', text)
        log_ref, log_flags = ref, flags
        log_version = version.group(1) if version else log_version
        variants.add((ref, tuple(flags)))
        for match in re.finditer(r'Configuration loaded from:?\s*(.+)', text):
            if match.group(1).strip() != DEFAULT_CONFIG:
                config_files.add(match.group(1).strip())
    if len(variants) > 1:
        notes.append('AC settings differ between per-sample logs %s; using one set' % sorted(variants))
    if config_files:
        # AC logs a config file's path, not its values, so they can't be carried over.
        notes.append('AC used a config file (%s). Its values aren\'t in the log.'
                     % ', '.join(sorted(config_files)))

    meta_version = meta_command = None
    for path in find_files(root, '_run_metadata.json'):
        try:
            with open(path) as handle:
                meta = json.load(handle)
        except ValueError:
            continue
        meta_version = meta_version or meta.get('AC_version')
        meta_command = meta_command or meta.get('AC_cmd')
        if meta_version and meta_command:
            break

    if log_ref:
        ref, flags, version, source = log_ref, log_flags, log_version, 'AC log'
        if meta_version not in (None, '', 'NA', 'None') and log_version and str(meta_version) != str(log_version):
            info.append('AC was rerun after the pipeline: the log says %s and run metadata says '
                        '%s. Using the log.' % (log_version, meta_version))
    elif meta_command:
        ref, flags = _parse_ac_command(meta_command)
        version, source = meta_version, 'run metadata'
        notes.append('No AC log, so these settings come from run metadata and may be older than '
                     'the results.')
    else:
        ref, flags, version, source = None, [], None, 'defaults'
        notes.append('No AC log or run metadata, so no settings could be recovered.')

    if ref:
        ref = REF_MAP.get(ref, ref)
    else:
        ref = REF_MAP.get(api_reference)
        notes.append('Reference taken from the site (%s).' % api_reference)
    if '--exclude_bed' in flags:
        i = flags.index('--exclude_bed')
        if not os.path.exists(flags[i + 1]):
            notes.append('Dropped --exclude_bed %s: the file isn\'t available.' % flags[i + 1])
            del flags[i:i + 2]
    return {'ref': ref, 'flags': flags, 'settings_ac_version': version, 'source': source,
            'notes': notes, 'info': info}


# ---- checks on built archives ----

_AMPLICON_PNG = re.compile(r'.+_amplicon\d+\.png$')
_AMPLICON_FILE = re.compile(r'.+_amplicon\d+(\.png|\.pdf|_graph\.txt|_cycles\.txt)$')
# These must round-trip unchanged. Classification tables change on purpose, so they aren't here.
_KEPT_SUFFIXES = ('_CNV_CALLS.bed', '_AA_CNV_SEEDS.bed', '_cnvkit_output.tar.gz', '_summary.txt',
                  '_sample_metadata.json', '_run_metadata.json')


def _member_names(tar_path):
    with tarfile.open(tar_path) as tar:
        return [os.path.basename(name) for name in tar.getnames()
                if not os.path.basename(name).startswith('._')]


def plots_in_archive(tar_path):
    return {name for name in _member_names(tar_path) if _AMPLICON_PNG.match(name)}


def plots_in_tree(root):
    """Amplicon plots in an unpacked tree, including inside per-sample AA tarballs."""
    found = set()
    for dirpath, _, names in os.walk(root):
        for name in names:
            if name.startswith('._'):
                continue
            if _AMPLICON_PNG.match(name):
                found.add(name)
            elif name.endswith('_AA_results.tar.gz'):
                try:
                    found |= plots_in_archive(os.path.join(dirpath, name))
                except tarfile.TarError:
                    pass
    return found


def kept_content(tar_path):
    return {name for name in _member_names(tar_path)
            if name.endswith(_KEPT_SUFFIXES) or _AMPLICON_FILE.match(name)}


def run_json_summary(tar_path):
    """The samples named in an archive's run.json, the counts the site will report for that file
    (`entries`, `features`), and its size."""
    with tarfile.open(tar_path) as tar:
        member = next((m for m in tar if os.path.basename(m.name) == 'run.json' and m.isfile()), None)
        if member is None:
            raise ProjectError('%s has no run.json' % tar_path)
        data = json.load(tar.extractfile(member))
    names = {feature.get('Sample name') for features in data.get('runs', {}).values()
             for feature in features}
    return {'samples': sorted(n for n in names if n), 'bytes': member.size,
            'entries': len(data.get('runs', {})),
            'features': sum(len(features) for features in data.get('runs', {}).values())}


def content_hashes(tar_path):
    """{sha256: size} for every file in an archive, read in one pass."""
    hashes = {}
    with tarfile.open(tar_path) as tar:
        for member in tar:
            if not member.isfile():
                continue
            digest = hashlib.sha256()
            source = tar.extractfile(member)
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
            hashes[digest.hexdigest()] = member.size
    return hashes


def new_content_bytes(old_tar, new_tar):
    """Bytes in the new archive that no file in the old one holds, a local estimate of what the
    site would write besides the archive itself."""
    old = content_hashes(old_tar)
    return sum(size for digest, size in content_hashes(new_tar).items() if digest not in old)
