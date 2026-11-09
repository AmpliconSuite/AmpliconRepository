"""Reading a site archive, checking AC's input against it, and swapping in AC's new output.

A site archive is already aggregated: `results/run.json` names each sample, `results/samples/`
holds their files, and AC's output is in one folder, `results/consolidated_classification/`.
All 27 keep-current projects on prod were laid out this way on 2026-11-09. The new archive is
the old one with that folder replaced, and the site aggregates it again when it takes it in.
"""

import hashlib
import json
import os
import re
import tarfile
from collections import defaultdict

from .plan import REF_MAP


class ProjectError(Exception):
    """This project can't continue. Other projects in the batch still can."""


def extract(tar_path, dest):
    """Unpack untrusted archive contents. The filter refuses paths that leave dest."""
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest, filter='data')


def find_files(root, suffix):
    found = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(suffix) and not name.startswith('.'):
                found.append(os.path.join(dirpath, name))
    return found


def samples_root(unpacked):
    """`results/samples/` of an unpacked site archive. ProjectError for another layout."""
    results = os.path.join(unpacked, 'results')
    for needed in ('run.json', 'samples'):
        if not os.path.exists(os.path.join(results, needed)):
            raise ProjectError('the archive has no results/%s, so it isn\'t laid out as the site\'s '
                               'aggregator writes archives today. Re-aggregate it on the site first.'
                               % needed)
    return os.path.join(results, 'samples')


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
            if aa and os.path.isdir(aa):
                paths['aa_dir'] = aa
            elif aa:
                paths['aa_not_a_dir'] = aa
            for key, field in (('cnv_calls_bed', 'CNV BED file'),
                               ('sample_meta', 'Sample metadata JSON'),
                               ('run_meta', 'Run metadata JSON')):
                path = _resolve(base, feature.get(field))
                if path:
                    paths[key] = path
            if paths:
                samples[name] = paths
    return samples


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
    """{sample: paths}, from run.json. Keeps samples with AA data or a CNV BED: a sample with
    only a CNV has no amplification, and it belongs in the archive as part of the denominator."""
    samples = discover_from_runjson(root)
    _attach_named_files(root, samples)
    kept = {name: paths for name, paths in samples.items()
            if 'aa_dir' in paths or 'aa_not_a_dir' in paths or paths.get('cnv_calls_bed')}
    for name, paths in kept.items():
        home = _sample_home(name, paths)
        if home:
            paths['home'] = home
    return kept


def _sample_home(sample, paths):
    """The directory named after this sample that holds its files, as in
    results/samples/<sample>/, or None in a layout without one."""
    for path in paths.values():
        directory = os.path.dirname(path)
        if os.path.basename(directory) == sample:
            return directory
    return None


def aux_dirs(root):
    """Directories marked with an AUX_DIR file. The aggregator copies them whole into
    other_files/, and they are the uploader's own files, so a rebuild must keep them."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath != root and 'AUX_DIR' in filenames:
            found.append(dirpath)
            dirnames[:] = []
    return sorted(found)


def structure_problems(samples):
    """Samples whose reconstruction directory AC can't use as it stands. The fix belongs on
    the site, so this only reports.

    - An amplicon's graph file more than once: AC searches recursively, so it would classify
      those amplicons twice and fail at its results table hours later. PCAWG on dev had a
      nested <sample>_AA_results copy in every sample (measured 2026-11-07).
    - An empty graph or cycles file: AC 2.0.1 refuses them. CUGA on dev has 9 samples whose
      only amplicon files are empty (measured 2026-11-07).
    - AA results that run.json names but that aren't a folder, such as a tarball: AC wouldn't
      see their amplicons. None of the 27 keep-current archives on prod had one (2026-11-09).
    """
    problems = []
    for sample, paths in sorted(samples.items()):
        if 'aa_not_a_dir' in paths:
            problems.append('%s: its AA results, %s, are not a folder'
                            % (sample, os.path.basename(paths['aa_not_a_dir'])))
        if 'aa_dir' not in paths:
            continue
        top = paths['aa_dir']
        seen = defaultdict(list)
        for path in find_files(top, '_graph.txt'):
            seen[os.path.basename(path)].append(os.path.relpath(os.path.dirname(path), top))
        repeated = sorted(name for name, dirs in seen.items() if len(dirs) > 1)
        if repeated:
            problems.append('%s: %d graph file(s) more than once, %s in %s'
                            % (sample, len(repeated), repeated[0], ' and '.join(seen[repeated[0]])))
        empty = sorted(os.path.basename(p) for p in find_files(top, '_graph.txt') + find_files(top, '_cycles.txt')
                       if os.path.getsize(p) == 0)
        if empty:
            problems.append('%s: %d empty AA file(s), first %s' % (sample, len(empty), empty[0]))
    return problems


def old_ac_dirs(root):
    """Every directory with AC profiles. A project may have one, or one per sample. AC output
    kept in an AUX folder, such as PCAWG's copy of an older AC run, is the uploader's record
    and not the version's calls."""
    aux = [d + os.sep for d in aux_dirs(root)]
    return sorted({os.path.dirname(p)
                   for p in find_files(root, '_amplicon_classification_profiles.tsv')
                   if not any(p.startswith(d) for d in aux)})


# ---- staging ----

def read_ac_input(path):
    """make_input.sh's pairs: [(sample, cycles path, graph path)]."""
    with open(path) as handle:
        return [tuple(line.rstrip('\n').split('\t')[:3]) for line in handle if line.strip()]


def ac_input_problems(pairs, samples):
    """How AC's input disagrees with the samples run.json names, checked before AC runs for
    hours. Empty means AC sees each of the archive's amplicons once.

    - an amplicon more than once, as from a nested copy of a sample's AA results
    - a sample run.json doesn't name, which AC would classify into a sample the site lacks
    - a sample with AA graph files that has no pairs: its raw `_cycles.txt` files are missing,
      as in an archive with only AC's annotated cycles, and every amplicon would become No amp
    """
    problems = []
    graphs = defaultdict(list)
    for sample, _, graph in pairs:
        graphs[os.path.basename(graph)].append(graph)
    repeated = sorted(name for name, paths in graphs.items() if len(paths) > 1)
    if repeated:
        problems.append('%d amplicon(s) found more than once, first %s in %s'
                        % (len(repeated), repeated[0], ' and '.join(graphs[repeated[0]])))
    named = {sample for sample, _, _ in pairs}
    unknown = sorted(named - set(samples))
    if unknown:
        problems.append('%d sample(s) with AA results that run.json doesn\'t name, first %s'
                        % (len(unknown), unknown[0]))
    unpaired = sorted(sample for sample, paths in samples.items()
                      if sample not in named and 'aa_dir' in paths
                      and find_files(paths['aa_dir'], '_graph.txt'))
    if unpaired:
        problems.append('%d sample(s) with AA graph files but no raw _cycles.txt, first %s'
                        % (len(unpaired), unpaired[0]))
    return problems


# What this tool writes into AC's folder, which isn't AC's output.
_OWN_FILES = ('ac_console.out',)


def swap_ac_output(unpacked, ac_dir, dest):
    """Write `dest`: the unpacked archive with its AC folder(s) replaced by `ac_dir`, as
    `results/consolidated_classification/`. Nothing else is changed or left out."""
    old = [d + os.sep for d in old_ac_dirs(unpacked)]
    partial = dest + '.partial'
    with tarfile.open(partial, 'w:gz') as tar:
        for dirpath, dirnames, names in os.walk(unpacked):
            dirnames.sort()
            dirnames[:] = [d for d in dirnames if os.path.join(dirpath, d) + os.sep not in old]
            for name in sorted(names):
                path = os.path.join(dirpath, name)
                if not any(path.startswith(d) for d in old):
                    tar.add(path, arcname=os.path.relpath(path, unpacked), recursive=False)
        for dirpath, dirnames, names in os.walk(ac_dir):
            dirnames.sort()
            for name in sorted(names):
                if dirpath == ac_dir and name in _OWN_FILES:
                    continue
                path = os.path.join(dirpath, name)
                tar.add(path, arcname=os.path.join('results', 'consolidated_classification',
                                                   os.path.relpath(path, ac_dir)), recursive=False)
    os.replace(partial, dest)


# ---- how AC was run ----

# Every AmpliconClassifier 2.0.1 option that changes the calls or the files it writes is carried
# over from the original run, so no project needs its settings looked up by hand.
AC_FLAGS_VALUED = ('--min_flow', '--min_size', '--decomposition_strictness', '--filter_pval',
                   '--exclude_bed')
AC_FLAGS_BOOL = ('--filter_similar', '--add_chr_tag', '--no_LC_filter', '--force',
                 '--no_bfbarchitect')
# Options that AC 2.0.1 already has on: it declares `--report_complexity` with default=True and
# "[Deprecated - on by default]". Recognised but not carried: passing one changes nothing, and
# a later AC that removes a deprecated option would refuse the command. Two logs differing only
# in one didn't run AC differently.
AC_FLAGS_DEFAULT_ON = ('--report_complexity',)
# Options that describe the machine or this tool's own run, not the classification. `--config`
# is reported separately, because its values aren't in the log.
AC_FLAGS_OF_THE_RUN = ('--ref', '--AA_results', '-i', '--input', '-o', '--jobs', '--bfb_threads',
                       '--verbose_classification', '--no_results_table', '--config', '-h', '--help',
                       '-v', '--version')
DEFAULT_CONFIG = 'default config'


def _parse_ac_command(command):
    """(reference, flags to carry over, options in the command that aren't carried)."""
    # Only what follows the script: a wrapper such as `conda run -n env` may come before it.
    words = command.split()
    start = next((i + 1 for i, word in enumerate(words) if word.endswith('amplicon_classifier.py')), 0)
    tokens = []
    for token in words[start:]:
        tokens += token.split('=', 1) if token.startswith('--') and '=' in token else [token]
    ref, flags, unknown = None, [], []
    for i, token in enumerate(tokens):
        if token == '--ref' and i + 1 < len(tokens):
            ref = tokens[i + 1]
        elif token in AC_FLAGS_DEFAULT_ON:
            continue
        elif token in AC_FLAGS_BOOL:
            flags.append(token)
        elif token in AC_FLAGS_VALUED and i + 1 < len(tokens):
            flags += [token, tokens[i + 1]]
        # A log line starts "<time> - INFO - Command: ...", so a lone "-" isn't an option.
        elif (re.match(r'^--?[A-Za-z][\w-]*$', token) and token not in AC_FLAGS_OF_THE_RUN
              and token not in unknown):
            unknown.append(token)
    return ref, flags, unknown


def recover_settings(root, api_reference):
    """How AC made these results: the AC log if present, then run metadata, then the API.

    A log wins over run metadata, because AC may have been rerun after the pipeline. Settings
    from run metadata can be older than the results beside them, and they're noted as such.

    `notes` stop the upload until a person accepts them: something about the run couldn't be
    carried over. `info` is shown for the review and stops nothing. Where the settings came from
    is `info`. On 2026-11-06 the rerun message fired on both real projects tried, because AC is
    commonly rerun after the pipeline. On 2026-11-09, 9 of prod's 26 rebuilt projects had no
    AC log, and in all 17 that had one, its reference matched the site's.
    """
    notes, info = [], []
    log_ref, log_version, log_flags = None, None, []
    variants, config_files, unknown_flags = set(), set(), set()
    logs = set(find_files(root, '.log')) | set(find_files(root, '_AA_AC_out.txt'))
    for path in sorted(logs):
        with open(path, errors='ignore') as handle:
            text = handle.read()
        command = next((line for line in text.splitlines()
                        if 'amplicon_classifier.py' in line and '--ref' in line
                        and 'make_input' not in line and 'make_results' not in line), None)
        if not command:
            continue
        ref, flags, unknown = _parse_ac_command(command)
        if not ref:
            continue
        unknown_flags.update(unknown)
        version = re.search(r'AmpliconClassifier\s+v?(\d+\.\d+\.\d+)', text)
        log_ref, log_flags = ref, flags
        log_version = version.group(1) if version else log_version
        variants.add((ref, tuple(flag for flag in flags if flag not in AC_FLAGS_DEFAULT_ON)))
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
        ref, flags, unknown = _parse_ac_command(meta_command)
        unknown_flags.update(unknown)
        version, source = meta_version, 'run metadata'
        info.append('No AC log, so these settings come from run metadata and may be older than '
                     'the results.')
    else:
        ref, flags, version, source = None, [], None, 'defaults'
        info.append('No AC log or run metadata, so AC ran with its defaults.')

    if unknown_flags:
        notes.append('The original AC command has %s, which this tool doesn\'t carry over.'
                     % ', '.join(sorted(unknown_flags)))
    if ref:
        ref = REF_MAP.get(ref, ref)
    else:
        ref = REF_MAP.get(api_reference)
        info.append('Reference taken from the site (%s).' % api_reference)
    if '--exclude_bed' in flags:
        i = flags.index('--exclude_bed')
        if not os.path.exists(flags[i + 1]):
            notes.append('Dropped --exclude_bed %s: the file isn\'t available.' % flags[i + 1])
            del flags[i:i + 2]
    return {'ref': ref, 'flags': flags, 'settings_ac_version': version, 'source': source,
            'notes': notes, 'info': info}


# ---- reading built archives ----

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


def context_calls(tar_path):
    """How many ecDNA context calls an archive holds. Plenty of archives legitimately have none."""
    calls = 0
    with tarfile.open(tar_path) as tar:
        for member in tar:
            name = os.path.basename(member.name)
            if member.isfile() and name.endswith('ecDNA_context_calls.tsv') and not name.startswith('._'):
                calls += sum(1 for line in tar.extractfile(member) if line.strip())
    return calls


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
