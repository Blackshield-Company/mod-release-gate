import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import zipfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / 'scripts'))
sys.path.insert(0, str(HERE))

import mod_release_gate as gate  # noqa: E402
from fixtures import FLAG_ESL, FLAG_ESM, make_mod, plugin_bytes, write  # noqa: E402

SCRIPT = REPO / 'scripts' / 'mod_release_gate.py'
CLEAN_ENV = {k: v for k, v in os.environ.items()
             if k not in ('GITHUB_REF_TYPE', 'GITHUB_REF_NAME', 'GITHUB_ACTIONS', 'MOD_CONFLICT_GRAPH_PATH')}


def run_gate(*argv):
    """Run main() in-process; return (exit code, result dict, stdout)."""
    out = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        json_path = os.path.join(tmp, 'r.json')
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                unittest.mock.patch.dict(os.environ, CLEAN_ENV, clear=True):
            code = gate.main(list(argv) + ['--json', json_path, '--no-git'])
        result = json.loads(Path(json_path).read_text()) if os.path.exists(json_path) else None
    return code, result, out.getvalue()


def codes(result, level=None):
    return {f['code'] for f in result['findings'] if level is None or f['level'] == level}




class PluginParsing(unittest.TestCase):
    def test_header_fields(self):
        data = plugin_bytes(masters=['Skyrim.esm', 'Update.esm'], flags=FLAG_ESL | FLAG_ESM, hedr=1.7,
                            new_ids=[0x800, 0x900], overrides=[0x01000ABC])
        info = gate.parse_plugin(data, 'A.esp')
        self.assertTrue(info.esl_flag and info.esm_flag and info.is_light)
        self.assertEqual(info.masters, ['Skyrim.esm', 'Update.esm'])
        self.assertEqual(info.hedr_version, 1.7)
        self.assertEqual(info.new_ids, [0x800, 0x900])
        self.assertEqual(info.overrides, 1)
        self.assertEqual(info.groups, 1)

    def test_esl_extension_is_light_without_flag(self):
        self.assertTrue(gate.parse_plugin(plugin_bytes(), 'A.esl').is_light)
        self.assertFalse(gate.parse_plugin(plugin_bytes(), 'A.esp').is_light)

    def test_rejects_garbage(self):
        for data in (b'', b'TES3' + b'\0' * 40, b'TES4' + b'\xff' * 20):
            with self.assertRaises(gate.PluginError):
                gate.parse_plugin(data, 'bad.esp')

    def test_rejects_truncated_records(self):
        data = plugin_bytes(new_ids=[0x800, 0x801])
        with self.assertRaises(gate.PluginError):
            gate.parse_plugin(data[:-5], 'cut.esp')

    def test_missing_hedr(self):
        data = b'TES4' + (0).to_bytes(4, 'little') * 5
        with self.assertRaisesRegex(gate.PluginError, 'HEDR'):
            gate.parse_plugin(data, 'nohedr.esp')

    def test_esl_ranges_per_game(self):
        self.assertEqual(gate.esl_range('skyrimse', 1.7), (0x800, 0xFFF))
        self.assertEqual(gate.esl_range('skyrimse', 1.71), (0x001, 0xFFF))
        self.assertEqual(gate.esl_range('fallout4', 0.95), (0x800, 0xFFF))
        self.assertEqual(gate.esl_range('fallout4', 1.0), (0x001, 0xFFF))


class PluginCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def plugins(self, *extra, game='skyrimse'):
        code, result, _ = run_gate('--root', str(self.root), '--game', game, '--skip', 'version,fomod,files,conflicts',
                                   *extra)
        return code, result

    def test_require_esl(self):
        write(self.root, 'Full.esp', plugin_bytes(masters=['Skyrim.esm'], new_ids=[0x800]))
        code, result = self.plugins('--esl', 'require')
        self.assertEqual(code, 1)
        self.assertIn('ESL-REQUIRED', codes(result, 'error'))

    def test_forbid_esl(self):
        write(self.root, 'Light.esp', plugin_bytes(masters=['Skyrim.esm'], flags=FLAG_ESL, new_ids=[0x800]))
        code, result = self.plugins('--esl', 'forbid')
        self.assertEqual(code, 1)
        self.assertIn('ESL-FORBIDDEN', codes(result, 'error'))
        self.assertEqual(self.plugins('--esl', 'any')[0], 0)

    def test_esl_range_legacy_header(self):
        write(self.root, 'Light.esp', plugin_bytes(masters=['Skyrim.esm'], flags=FLAG_ESL, hedr=1.7,
                                                   new_ids=[0x7FF, 0x800]))
        code, result = self.plugins()
        self.assertEqual(code, 1)
        self.assertIn('ESL-RANGE', codes(result, 'error'))

    def test_esl_extended_range_with_171(self):
        write(self.root, 'Light.esp', plugin_bytes(masters=['Skyrim.esm'], flags=FLAG_ESL, hedr=1.71,
                                                   new_ids=[0x001, 0x7FF, 0xFFF]))
        code, result = self.plugins()
        self.assertEqual(code, 0, result['findings'])
        self.assertIn('ESL-OK', codes(result, 'notice'))

    def test_esl_above_fff(self):
        write(self.root, 'Light.esl', plugin_bytes(masters=['Fallout4.esm'], flags=FLAG_ESL | FLAG_ESM, hedr=1.0,
                                                   new_ids=[0x1000]))
        code, result = self.plugins(game='fallout4')
        self.assertEqual(code, 1)
        self.assertIn('ESL-RANGE', codes(result, 'error'))
        self.assertIn('ESL-NEXT-ID', codes(result, 'warning'))

    def test_esl_budget(self):
        write(self.root, 'Big.esp', plugin_bytes(masters=['Skyrim.esm'], flags=FLAG_ESL, hedr=1.7,
                                                 new_ids=list(range(0x800, 0x1000)) + [0x800]))
        _code, result = self.plugins()
        self.assertIn('ESL-BUDGET', codes(result, 'error'))

    def test_overrides_do_not_count_against_esl(self):
        write(self.root, 'Patch.esp', plugin_bytes(masters=['Skyrim.esm'], flags=FLAG_ESL, hedr=1.7,
                                                   overrides=[0x00012E46, 0x00000014]))
        self.assertEqual(self.plugins()[0], 0)

    def test_non_light_plugin_is_not_range_checked(self):
        write(self.root, 'Full.esp', plugin_bytes(masters=['Skyrim.esm'], hedr=1.7, new_ids=[0x5000]))
        self.assertEqual(self.plugins()[0], 0)

    def test_wrong_game_master_and_hedr(self):
        write(self.root, 'Port.esp', plugin_bytes(masters=['Fallout4.esm'], hedr=1.0, new_ids=[0x800]))
        code, result = self.plugins()
        self.assertEqual(code, 1)
        self.assertIn('PLUGIN-WRONG-GAME', codes(result, 'error'))
        hedr = [f for f in result['findings'] if f['code'] == 'PLUGIN-HEDR'][0]
        self.assertIn('Fallout 4', hedr['detail'])

    def test_bad_master_index(self):
        write(self.root, 'Odd.esp', plugin_bytes(masters=['Skyrim.esm'], raw_form_ids=[0x05000800]))
        self.assertIn('PLUGIN-FORMID', codes(self.plugins()[1], 'error'))

    def test_unparseable_plugin(self):
        write(self.root, 'Broken.esp', b'not a plugin at all, just text')
        code, result = self.plugins()
        self.assertEqual(code, 1)
        self.assertIn('PLUGIN-PARSE', codes(result, 'error'))

    def test_explicit_plugin_globs(self):
        write(self.root, 'Data/A.esp', plugin_bytes(masters=['Skyrim.esm']))
        write(self.root, 'tests/Ignored.esp', b'garbage')
        code, result = self.plugins('--plugins', 'Data/*.esp\nMissing.esp')
        self.assertEqual(code, 1)
        self.assertEqual([p['plugin'] for p in result['plugins']], ['Data/A.esp'])
        self.assertIn('PLUGIN-MISSING', codes(result, 'error'))

    def test_discovery_skips_test_dirs(self):
        write(self.root, 'A.esp', plugin_bytes(masters=['Skyrim.esm']))
        write(self.root, 'tests/fixtures/Bad.esp', b'garbage')
        code, result = self.plugins()
        self.assertEqual(code, 0)
        self.assertEqual(len(result['plugins']), 1)

    def test_no_plugins_is_a_warning(self):
        code, result = self.plugins()
        self.assertEqual(code, 0)
        self.assertIn('PLUGIN-NONE', codes(result, 'warning'))
        self.assertEqual(self.plugins('--fail-on', 'warning')[0], 1)


class VersionCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def version(self, *extra):
        code, result, _ = run_gate('--root', str(self.root), '--game', 'skyrimse',
                                   '--skip', 'plugins,files,conflicts', *extra)
        return code, result

    def test_changelog_top_skips_unreleased_and_fences(self):
        text = '# Changelog\n```\n## 9.9.9\n```\n## Unreleased\n## [2.1.0-rc.1] - 2026-02-02\n## 2.0.0\n'
        self.assertEqual(gate.changelog_top(text)[0], '2.1.0-rc.1')
        self.assertEqual(gate.changelog_top('## Version 3.4 (beta)\n')[0], '3.4')
        self.assertEqual(gate.changelog_top('# Notes\nnothing\n'), (None, None))

    def test_normalize(self):
        self.assertEqual(gate.normalize_version('v1.2.0'), '1.2')
        self.assertEqual(gate.normalize_version('1.02'), '1.2')
        self.assertEqual(gate.normalize_version('1.2.0-RC1+build'), '1.2-rc1')
        self.assertNotEqual(gate.normalize_version('1.2.1'), gate.normalize_version('1.2'))

    def test_all_agree(self):
        make_mod(self.root, version='1.2.0')
        code, result = self.version('--tag', 'v1.2.0')
        self.assertEqual(code, 0, result['findings'])
        self.assertEqual(len(result['versions']), 3)

    def test_tag_mismatch(self):
        make_mod(self.root, version='1.2.0')
        code, result = self.version('--tag', 'v1.3.0')
        self.assertEqual(code, 1)
        self.assertEqual(len([f for f in result['findings'] if f['code'] == 'VER-MISMATCH']), 2)

    def test_fomod_version_drift(self):
        make_mod(self.root, version='1.2.0')
        write(self.root, 'fomod/info.xml', '<fomod><Name>x</Name><Version>1.1.0</Version></fomod>')
        code, result = self.version('--tag', 'v1.2.0')
        self.assertEqual(code, 1)
        drift = [f for f in result['findings'] if f['code'] == 'VER-MISMATCH']
        self.assertEqual(drift[0]['subject'], 'fomod info.xml')

    def test_custom_prefix_and_changelog_path(self):
        make_mod(self.root, version='0.4.0', changelog=False, fomod=False)
        write(self.root, 'docs/HISTORY.md', '# History\n\n## 0.4.0\n')
        code, result = self.version('--tag', 'mymod-0.4.0', '--tag-prefix', 'mymod-', '--changelog', 'docs/HISTORY.md')
        self.assertEqual(code, 0, result['findings'])

    def test_tag_from_github_env(self):
        make_mod(self.root, version='1.2.0')
        env = dict(CLEAN_ENV, GITHUB_REF_TYPE='tag', GITHUB_REF_NAME='v9.0.0')
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            tag, origin = gate.resolve_tag(self.root, '', False)
        self.assertEqual((tag, origin), ('v9.0.0', 'GITHUB_REF_NAME'))

    def test_no_tag_is_a_notice_unless_required(self):
        make_mod(self.root)
        self.assertEqual(self.version()[0], 0)
        code, result = self.version('--require-tag')
        self.assertEqual(code, 1)
        self.assertIn('VER-NO-TAG', codes(result, 'error'))

    def test_missing_changelog_with_tag(self):
        make_mod(self.root, changelog=False, fomod=False)
        code, result = self.version('--tag', 'v1.2.0')
        self.assertEqual(code, 1)
        self.assertIn('VER-CHANGELOG-MISSING', codes(result, 'error'))

    def test_changelog_without_versions(self):
        make_mod(self.root, changelog=False, fomod=False)
        write(self.root, 'CHANGELOG.md', '# Changelog\n\nStuff happened.\n')
        self.assertIn('VER-CHANGELOG-ENTRY', codes(self.version('--tag', 'v1.0.0')[1], 'error'))

    def test_git_tag_at_head(self):
        make_mod(self.root, version='2.0.0')
        git = ['git', '-C', str(self.root), '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
               '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false']
        try:
            for cmd in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'init'], ['tag', 'v2.0.0']):
                subprocess.run(git + cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest('git unavailable')
        self.assertEqual(gate.git_tag(self.root), 'v2.0.0')


class FilesCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_mod(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def files(self, *extra, game='skyrimse'):
        code, result, _ = run_gate('--root', str(self.root), '--game', game,
                                   '--skip', 'plugins,version,fomod,conflicts', *extra)
        return code, result

    def test_clean(self):
        code, result = self.files()
        self.assertEqual(code, 0)
        self.assertIn('FILE-OK', codes(result))

    def test_forbidden_kinds(self):
        for relpath, code in (('Skyrim.esm', 'FILE-GAME-MASTER'), ('Data/dawnguard.ESM', 'FILE-GAME-MASTER'),
                              ('DLCNukaWorld.esm', 'FILE-GAME-MASTER'), ('ccBGSSSE001-Fish.esm', 'FILE-CREATION-CLUB'),
                              ('ccbgsfo4044-hellfirepowerarmor.esl', 'FILE-CREATION-CLUB'),
                              ('TestMod - Textures.bsa', 'FILE-ARCHIVE'), ('Main.BA2', 'FILE-ARCHIVE'),
                              ('Source/Scripts/TM_Quest.psc', 'FILE-PAPYRUS-SOURCE')):
            write(self.root, relpath, b'x')
        code, result = self.files()
        self.assertEqual(code, 1)
        found = {(f['subject'], f['code']) for f in result['findings'] if f['level'] == 'error'}
        self.assertIn(('Skyrim.esm', 'FILE-GAME-MASTER'), found)
        self.assertIn(('Data/dawnguard.ESM', 'FILE-GAME-MASTER'), found)
        self.assertIn(('DLCNukaWorld.esm', 'FILE-GAME-MASTER'), found)
        self.assertIn(('ccBGSSSE001-Fish.esm', 'FILE-CREATION-CLUB'), found)
        self.assertIn(('ccbgsfo4044-hellfirepowerarmor.esl', 'FILE-CREATION-CLUB'), found)
        self.assertIn(('TestMod - Textures.bsa', 'FILE-ARCHIVE'), found)
        self.assertIn(('Main.BA2', 'FILE-ARCHIVE'), found)
        self.assertIn(('Source/Scripts/TM_Quest.psc', 'FILE-PAPYRUS-SOURCE'), found)

    def test_allowlist(self):
        write(self.root, 'Source/Scripts/TM_Quest.psc', b'x')
        write(self.root, 'TestMod - Main.bsa', b'x')
        code, result = self.files('--allow', 'source/scripts/*.psc\nTestMod - *.bsa')
        self.assertEqual(code, 0, result['findings'])
        self.assertEqual(len([f for f in result['findings'] if f['code'] == 'FILE-ALLOWED']), 2)

    def test_extra_forbid_and_git_dir_ignored(self):
        write(self.root, 'notes.tmp', b'x')
        write(self.root, '.git/objects/Skyrim.esm', b'x')
        code, result = self.files('--forbid', '*.tmp')
        self.assertEqual(code, 1)
        self.assertEqual({f['subject'] for f in result['findings'] if f['level'] == 'error'}, {'notes.tmp'})

    def test_scan_zip(self):
        zpath = self.root / 'dist' / 'TestMod.zip'
        zpath.parent.mkdir()
        with zipfile.ZipFile(zpath, 'w') as archive:
            archive.writestr('TestMod.esp', b'x')
            archive.writestr('Update.esm', b'x')
        code, result = self.files('--scan', 'dist/TestMod.zip')
        self.assertEqual(code, 1)
        self.assertEqual([f['subject'] for f in result['findings'] if f['level'] == 'error'],
                         ['dist/TestMod.zip!/Update.esm'])

    def test_bad_scan_path_is_usage_error(self):
        self.assertEqual(self.files('--scan', 'nope')[0], 2)


class FomodCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def fomod(self, *extra):
        code, result, _ = run_gate('--root', str(self.root), '--game', 'skyrimse',
                                   '--skip', 'plugins,version,files,conflicts', *extra)
        return code, result

    def test_clean(self):
        make_mod(self.root)
        code, result = self.fomod()
        self.assertEqual(code, 0, result['findings'])
        ok = [f for f in result['findings'] if f['code'] == 'FOMOD-OK'][0]
        self.assertIn('4 referenced', ok['detail'])

    def test_case_insensitive_sources(self):
        make_mod(self.root, plugin='TestMod.esp')
        text = (self.root / 'fomod/ModuleConfig.xml').read_text().replace('optional\\textures', 'OPTIONAL\\Textures')
        write(self.root, 'fomod/ModuleConfig.xml', text)
        self.assertEqual(self.fomod()[0], 0)

    def test_missing_sources(self):
        make_mod(self.root)
        (self.root / 'TestMod.esp').unlink()
        (self.root / 'fomod/images/banner.png').unlink()
        code, result = self.fomod()
        self.assertEqual(code, 1)
        missing = {f['subject'] for f in result['findings'] if f['code'] == 'FOMOD-MISSING-SOURCE'}
        self.assertEqual(missing, {'TestMod.esp', 'fomod/images/banner.png'})

    def test_folder_must_be_directory(self):
        make_mod(self.root)
        import shutil
        shutil.rmtree(self.root / 'optional')
        write(self.root, 'optional/textures', b'file not folder')
        self.assertIn('FOMOD-MISSING-SOURCE', codes(self.fomod()[1], 'error'))

    def test_malformed_xml(self):
        make_mod(self.root)
        write(self.root, 'fomod/ModuleConfig.xml', '<config><moduleName>x</moduleName>')
        write(self.root, 'fomod/info.xml', '<fomod><Name>x</Name>')
        code, result = self.fomod()
        self.assertEqual(code, 1)
        self.assertEqual(len([f for f in result['findings'] if f['code'] == 'FOMOD-XML']), 2)

    def test_path_escape(self):
        make_mod(self.root)
        text = (self.root / 'fomod/ModuleConfig.xml').read_text().replace(
            'source="TestMod.esp"', 'source="..\\..\\etc\\passwd"')
        write(self.root, 'fomod/ModuleConfig.xml', text)
        self.assertIn('FOMOD-BAD-PATH', codes(self.fomod()[1], 'error'))

    def test_utf16_info_xml(self):
        make_mod(self.root)
        write(self.root, 'fomod/info.xml',
              '<?xml version="1.0" encoding="UTF-16"?><fomod><Name>x</Name><Version>1.2.0</Version></fomod>'
              .encode('utf-16'))
        self.assertEqual(self.fomod()[0], 0)

    def test_none_is_notice_and_explicit_missing_is_error(self):
        self.assertEqual(self.fomod()[0], 0)
        code, result = self.fomod('--fomod', 'package/fomod')
        self.assertEqual(code, 1)
        self.assertIn('FOMOD-MISSING', codes(result, 'error'))

    def test_nested_package(self):
        make_mod(self.root / 'package')
        code, result = self.fomod()
        self.assertEqual(code, 0, result['findings'])


STUB = '''
def load_config(path):
    return {"conflicts_fail_on": "collision", "implicit": []}

def run(target, config, release=None, against=(), tag=None, use_git=True, fail_on=None):
    assert config["implicit"], "game masters must be passed as implicit"
    found = []
    if against:
        found.append({"check": "conflicts", "level": "blocker", "kind": "record-override",
                      "subject": "Other", "detail": "both override 00012E46 (%s)" % config["conflicts_fail_on"]})
    found.append({"check": "masters", "level": "blocker", "kind": "undeclared-master", "subject": "X.esm",
                  "detail": "undeclared"})
    found.append({"check": "version", "level": "blocker", "kind": "ignored", "subject": "-", "detail": "-"})
    return {"findings": found, "load_order": ["Skyrim.esm", "TestMod.esp"]}
'''


class ConflictsCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'mod'
        make_mod(self.root)
        self.stub = Path(self.tmp.name) / 'stub'
        write(self.stub, 'modconflict/__init__.py', '')
        write(self.stub, 'modconflict/modcheck.py', STUB)
        sys.modules.pop('modconflict', None)
        sys.modules.pop('modconflict.modcheck', None)

    def tearDown(self):
        for name in ('modconflict', 'modconflict.modcheck'):
            sys.modules.pop(name, None)
        sys.path[:] = [p for p in sys.path if p != str(self.stub.resolve())]
        self.tmp.cleanup()

    def conflicts(self, *extra):
        code, result, _ = run_gate('--root', str(self.root), '--game', 'skyrimse',
                                   '--skip', 'plugins,version,files,fomod', *extra)
        return code, result

    def test_unavailable_degrades(self):
        with unittest.mock.patch.object(gate, 'load_modconflict', return_value=(None, 'ImportError: nope')):
            code, result = self.conflicts()
            self.assertEqual(code, 0)
            self.assertIn('CONFLICTS-UNAVAILABLE', codes(result, 'notice'))
            code, result = self.conflicts('--require-conflicts')
            self.assertEqual(code, 1)

    def test_stub_engine_findings(self):
        other = Path(self.tmp.name) / 'other'
        make_mod(other, plugin='Other.esp')
        code, result = self.conflicts('--mod-conflict-graph', str(self.stub), '--against', str(other))
        self.assertEqual(code, 1)
        self.assertIn('CONFLICT-RECORD-OVERRIDE', codes(result, 'error'))
        self.assertIn('MASTER-UNDECLARED-MASTER', codes(result, 'warning'))
        self.assertNotIn('IGNORED', ' '.join(codes(result)))
        self.assertEqual(result['load_order'], ['Skyrim.esm', 'TestMod.esp'])

    def test_advisory_downgrades(self):
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        code, result = self.conflicts('--mod-conflict-graph', str(self.stub), '--against', str(other),
                                      '--advisory', 'conflicts')
        self.assertEqual(code, 0)
        self.assertIn('CONFLICT-RECORD-OVERRIDE', codes(result, 'warning'))

    def test_against_must_exist(self):
        self.assertEqual(self.conflicts('--mod-conflict-graph', str(self.stub), '--against', 'missing')[0], 2)

    def test_engine_crash_is_a_warning(self):
        write(self.stub, 'modconflict/modcheck.py', 'def load_config(p):\n    raise RuntimeError("boom")\n')
        code, result = self.conflicts('--mod-conflict-graph', str(self.stub))
        self.assertEqual(code, 0)
        self.assertIn('CONFLICTS-ERROR', codes(result, 'warning'))


@unittest.skipUnless(os.environ.get('MOD_CONFLICT_GRAPH_PATH') or Path('/workspace/mod-conflict-graph/modconflict').is_dir(),
                     'set MOD_CONFLICT_GRAPH_PATH to a mod-conflict-graph checkout to run')
class RealModConflictGraph(unittest.TestCase):
    """Integration with a real mod-conflict-graph checkout (run in a subprocess for isolation)."""

    def test_record_override_collision(self):
        mcg = os.environ.get('MOD_CONFLICT_GRAPH_PATH') or '/workspace/mod-conflict-graph'
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, 'Alpha'), Path(tmp, 'Beta')
            make_mod(a, plugin='Alpha.esp', fomod=False)
            make_mod(b, plugin='Beta.esp', fomod=False)
            out = Path(tmp, 'r.json')
            proc = subprocess.run([sys.executable, str(SCRIPT), '--root', str(a), '--game', 'skyrimse', '--no-git',
                                   '--skip', 'plugins,version,files,fomod', '--mod-conflict-graph', mcg,
                                   '--against', str(b), '--json', str(out), '--quiet'],
                                  env=CLEAN_ENV, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            result = json.loads(out.read_text())
            self.assertNotIn('CONFLICTS-UNAVAILABLE', codes(result), proc.stderr)
            self.assertNotIn('CONFLICTS-ERROR', codes(result), result['findings'])
            self.assertTrue(any(c.startswith('CONFLICT-') and c != 'CONFLICTS-OK' for c in codes(result)),
                            result['findings'])
            self.assertEqual(proc.returncode, 1, result['findings'])


class EndToEnd(unittest.TestCase):
    def test_clean_mod_passes_for_both_games(self):
        for game in ('skyrimse', 'fallout4'):
            with tempfile.TemporaryDirectory() as tmp:
                make_mod(tmp, game=game)
                code, result, out = run_gate('--root', tmp, '--game', game, '--tag', 'v1.2.0', '--esl', 'require',
                                             '--skip', 'conflicts')
                self.assertEqual(code, 0, result['findings'])
                self.assertEqual(result['result'], 'pass')
                self.assertIn('mod-release-gate: PASS', out)

    def test_cli_outputs_summary_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_mod(tmp)
            write(tmp, 'Skyrim.esm', b'x')
            summary, output = Path(tmp, 'summary.md'), Path(tmp, 'gh_output')
            summary.write_text('previous step\n')
            proc = subprocess.run([sys.executable, str(SCRIPT), '--root', tmp, '--game', 'skyrimse', '--tag', 'v1.2.0',
                                   '--skip', 'conflicts', '--no-git', '--summary', str(summary),
                                   '--github-output', str(output), '--quiet', '--allow', 'summary.md'],
                                  env=dict(CLEAN_ENV, GITHUB_ACTIONS='true'), timeout=60,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn('::error title=mod-release-gate files::FILE-GAME-MASTER Skyrim.esm', proc.stdout)
            text = summary.read_text()
            self.assertTrue(text.startswith('previous step\n## mod-release-gate: FAIL'))
            self.assertIn('| files | FAIL | 1 | 0 |', text)
            self.assertIn('| fomod | PASS |', text)
            self.assertIn('| conflicts | skipped |', text)
            self.assertIn('result=fail\nerrors=1\nwarnings=0\n', output.read_text())

    def test_fail_on_never(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_mod(tmp)
            write(tmp, 'x.bsa', b'x')
            code, result, _ = run_gate('--root', tmp, '--game', 'skyrimse', '--fail-on', 'never', '--skip', 'conflicts')
            self.assertEqual((code, result['result']), (0, 'pass'))

    def test_usage_errors(self):
        self.assertEqual(run_gate('--root', '/nonexistent/dir', '--game', 'skyrimse')[0], 2)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_gate('--root', tmp, '--game', 'skyrimse', '--skip', 'bogus')[0], 2)

    def test_markdown_escaping(self):
        result = {'game': 'skyrimse', 'fail_on': 'error', 'result': 'fail', 'checks': dict.fromkeys(gate.CHECKS, 'pass'),
                  'findings': [gate.finding('files', 'error', 'X', 'a|b<script>', 'd\ne')],
                  'plugins': [], 'versions': [], 'load_order': []}
        text = gate.render(result)
        self.assertIn('a\\|b&lt;script>', text)
        self.assertNotIn('<script>', text)


if __name__ == '__main__':
    unittest.main()
