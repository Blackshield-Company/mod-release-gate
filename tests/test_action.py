"""Exercise action.yml's composite steps with bash, the way the runner executes them."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

from fixtures import make_mod, write  # noqa: E402

ACTION = (REPO / 'action.yml').read_text(encoding='utf-8')


def step_script(step_id):
    """Return the dedented `run: |` block of the step with this id."""
    lines = ACTION.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == '- id: %s' % step_id)
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == 'run: |')
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body = []
    for line in lines[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return '\n'.join(body) + '\n'


def input_defaults():
    block = ACTION.split('\ninputs:\n', 1)[1].split('\noutputs:\n', 1)[0]
    defaults, current = {}, None
    for line in block.splitlines():
        match = re.match(r'^  ([a-z-]+):$', line)
        if match:
            current = match.group(1)
            defaults[current] = ''
        match = re.match(r"^    default: '(.*)'$", line)
        if match and current:
            defaults[current] = match.group(1)
    return defaults


@unittest.skipUnless(shutil.which('bash'), 'bash is required')
class CompositeAction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.mod = self.base / 'mod'
        make_mod(self.mod)
        self.output = self.base / 'output'
        self.summary = self.base / 'summary'
        self.output.write_text('')
        self.summary.write_text('')
        (self.base / 'runner').mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **inputs):
        values = input_defaults()
        values.update({'game': 'skyrimse', 'path': str(self.mod), 'tag': 'v1.2.0', 'skip': 'conflicts'})
        values.update({k.replace('_', '-'): v for k, v in inputs.items()})
        env = {k: v for k, v in os.environ.items() if not k.startswith(('GITHUB_', 'GATE_', 'MCG_'))}
        env.update(GITHUB_ACTION_PATH=str(REPO), GITHUB_OUTPUT=str(self.output),
                   GITHUB_STEP_SUMMARY=str(self.summary), RUNNER_TEMP=str(self.base / 'runner'),
                   GITHUB_REF_TYPE='branch', GITHUB_REF_NAME='main', MOD_CONFLICT_GRAPH_PATH='')
        # Map every `NAME: ${{ inputs.x }}` env line in action.yml to the input value.
        for name, key in re.findall(r'^\s+([A-Z_]+): \$\{\{ inputs\.([a-z-]+) \}\}$', ACTION, re.M):
            env[name] = values[key]
        return env

    def run_step(self, step_id, **inputs):
        return subprocess.run(['bash', '-c', step_script(step_id)], env=self.env(**inputs), timeout=120,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def outputs(self):
        return dict(line.split('=', 1) for line in self.output.read_text().splitlines() if '=' in line)

    def test_inputs_are_all_wired(self):
        referenced = set(re.findall(r'inputs\.([a-z-]+)', ACTION))
        self.assertEqual(referenced, set(input_defaults()))

    def test_pass(self):
        proc = self.run_step('gate', esl='require')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = self.outputs()
        self.assertEqual((out['result'], out['errors']), ('pass', '0'))
        self.assertTrue(Path(out['report']).is_file())
        self.assertTrue(Path(out['json']).is_file())
        self.assertIn('mod-release-gate: PASS', self.summary.read_text())

    def test_fail_with_multiline_inputs(self):
        write(self.mod, 'Scripts/Source/A.psc', b'x')
        write(self.mod, 'Dawnguard.esm', b'x')
        write(self.mod, 'build.log', b'x')
        proc = self.run_step('gate', allow='Scripts/Source/*.psc\n', forbid='*.log\n*.tmp', plugins='TestMod.esp\n')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        out = self.outputs()
        self.assertEqual((out['result'], out['errors']), ('fail', '2'))
        self.assertIn('FILE-GAME-MASTER', self.summary.read_text())

    def test_tag_from_ref(self):
        env_tag = self.env(tag='')
        env_tag.update(GITHUB_REF_TYPE='tag', GITHUB_REF_NAME='v9.9.9')
        proc = subprocess.run(['bash', '-c', step_script('gate')], env=env_tag, timeout=120,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('VER-MISMATCH', self.summary.read_text())

    def test_usage_error_still_sets_result(self):
        proc = self.run_step('gate', path=str(self.base / 'missing'))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.outputs()['result'], 'fail')

    def test_locate_with_local_checkout(self):
        checkout = self.base / 'mcg'
        write(checkout, 'modconflict/__init__.py', '')
        proc = self.run_step('mcg', skip='', mod_conflict_graph_path=str(checkout), mod_conflict_graph_source='')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.outputs()['path'], str(checkout))

    def test_locate_degrades_without_source(self):
        proc = self.run_step('mcg', skip='', mod_conflict_graph_path='', mod_conflict_graph_source='')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.outputs()['path'], '')

    def test_locate_skipped(self):
        proc = self.run_step('mcg', skip='plugins, conflicts', mod_conflict_graph_source='file:///nonexistent')
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.outputs()['path'], '')


if __name__ == '__main__':
    unittest.main()
