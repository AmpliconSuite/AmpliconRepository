"""Per-machine settings, kept in an ignored file because this repo is public."""

import dataclasses
import json
import os

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'machine.json')


class NotSetUp(Exception):
    pass


@dataclasses.dataclass
class Machine:
    workdir: str
    jobs: int
    bfb_threads: int
    token_files: dict = dataclasses.field(default_factory=dict)

    @property
    def env_dir(self):
        return os.path.join(self.workdir, 'env')

    @property
    def python(self):
        return os.path.join(self.env_dir, 'bin', 'python')

    @property
    def ac_script(self):
        return os.path.join(self.env_dir, 'bin', 'amplicon_classifier.py')

    @property
    def projects_dir(self):
        return os.path.join(self.workdir, 'projects')

    @property
    def output_dir(self):
        return os.path.join(self.workdir, 'output')

    @property
    def plan_path(self):
        return os.path.join(self.workdir, 'plan.json')

    def token(self, site_name):
        path = self.token_files.get(site_name)
        if not path:
            return None
        with open(os.path.expanduser(path)) as handle:
            return handle.read().strip() or None

    def save(self, path=DEFAULT_PATH):
        with open(path, 'w') as handle:
            json.dump(dataclasses.asdict(self), handle, indent=2)
            handle.write('\n')

    @classmethod
    def load(cls, path=DEFAULT_PATH):
        try:
            with open(path) as handle:
                return cls(**json.load(handle))
        except FileNotFoundError:
            raise NotSetUp('%s is missing. Run `python -m amprepo_reclassify setup` first.' % path)
