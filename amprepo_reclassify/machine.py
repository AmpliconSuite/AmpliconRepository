"""Per-machine settings, kept in an ignored file because this repo is public."""

import dataclasses
import json
import os

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'machine.json')

LOCAL = 'local'
SLURM = 'slurm'


class NotSetUp(Exception):
    pass


@dataclasses.dataclass
class Machine:
    workdir: str
    jobs: int
    bfb_threads: int
    token_files: dict = dataclasses.field(default_factory=dict)
    # A checkout of AC at the pinned tag. AC runs from source because a release is tagged on
    # GitHub before it reaches any package index.
    ac_src: str = None
    # The Python environment for AC's dependencies and the aggregator: a named conda environment,
    # or an interpreter's own environment. With neither, the one `setup` made in the workdir.
    conda_env: str = None
    conda_sh: str = None
    python_path: str = None
    # Where projects run: {'kind': 'local'}, or {'kind': 'slurm', ...} for one Slurm job each.
    executor: dict = dataclasses.field(default_factory=lambda: {'kind': LOCAL})

    @property
    def env_dir(self):
        return os.path.join(self.workdir, 'env')

    @property
    def python(self):
        """The interpreter to call. Under a conda environment, its own `python` once activated."""
        if self.conda_env:
            return 'python'
        return self.python_path or os.path.join(self.env_dir, 'bin', 'python')

    def tool_prefix(self):
        """What a tool's command starts with, to run it inside the environment."""
        if not self.conda_env:
            return []
        return ['bash', '--noprofile', '--norc', '-c',
                'source "$0" && conda activate "$1" && shift && exec "$@"',
                self.conda_sh, self.conda_env]

    @property
    def ac_script(self):
        return os.path.join(self.ac_src, 'amplicon_classifier.py')

    @property
    def projects_dir(self):
        return os.path.join(self.workdir, 'projects')

    @property
    def output_dir(self):
        return os.path.join(self.workdir, 'output')

    @property
    def plan_path(self):
        return os.path.join(self.workdir, 'plan.json')

    @property
    def executor_kind(self):
        return (self.executor or {}).get('kind', LOCAL)

    def token(self, site_name):
        path = self.token_files.get(site_name)
        if not path:
            return None
        with open(os.path.expanduser(path)) as handle:
            return handle.read().strip() or None

    def to_dict(self):
        return dataclasses.asdict(self)

    def save(self, path=DEFAULT_PATH):
        with open(path, 'w') as handle:
            json.dump(self.to_dict(), handle, indent=2)
            handle.write('\n')

    @classmethod
    def load(cls, path=DEFAULT_PATH):
        try:
            with open(path) as handle:
                return cls(**json.load(handle))
        except FileNotFoundError:
            raise NotSetUp('%s is missing. Run `python -m amprepo_reclassify setup` first.' % path)
