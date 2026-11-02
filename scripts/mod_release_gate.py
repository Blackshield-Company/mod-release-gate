#!/usr/bin/env python3
"""mod-release-gate: pre-release checks for Skyrim SE and Fallout 4 mod repositories.

Python 3.8+ standard library only. Every input is read, never modified. Git is
queried read-only (``git describe``) and only when no tag is supplied.

Checks
  plugins    TES4 header parse, ESL flag policy, ESL form-ID range and record budget,
             HEDR version per game, master index sanity.
  version    git tag == CHANGELOG top entry (== FOMOD info.xml <Version> when present).
  files      no Bethesda masters, Creation Club content, .bsa/.ba2 archives or .psc
             sources, unless allowlisted.
  fomod      fomod/info.xml and fomod/ModuleConfig.xml are well-formed and every file,
             folder and image they reference exists.
  conflicts  plugin conflict report from mod-conflict-graph when it is importable;
             skipped with a notice otherwise.

Exit codes: 0 pass, 1 fail, 2 usage or input error.
"""

import argparse
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

__version__ = '1.0.0'

CHECKS = ('plugins', 'version', 'files', 'fomod', 'conflicts')
LEVELS = ('error', 'warning', 'notice')
PLUGIN_EXTS = ('.esp', '.esm', '.esl')
SKIP_DIRS = frozenset(('.git', 'node_modules', '__pycache__'))
PLUGIN_SKIP_DIRS = SKIP_DIRS | frozenset(('tests', 'test', 'fixtures'))

FLAG_ESM = 0x1
FLAG_LOCALIZED = 0x80
FLAG_ESL = 0x200
FLAG_COMPRESSED = 0x40000

GAMES = {
    'skyrimse': {
        'title': 'Skyrim Special Edition',
        'masters': ('Skyrim.esm', 'Update.esm', 'Dawnguard.esm', 'HearthFires.esm', 'Dragonborn.esm',
                    '_ResourcePack.esl'),
        'hedr': (1.7, 1.71),
        # Skyrim SE 1.6.1130+ reads ESL object IDs 0x001-0xFFF when HEDR is 1.71.
        'extended_esl_hedr': 1.71,
        'cc': re.compile(r'^cc[a-z]{2,8}sse\d{3}', re.I),
    },
    'fallout4': {
        'title': 'Fallout 4',
        'masters': ('Fallout4.esm', 'DLCRobot.esm', 'DLCworkshop01.esm', 'DLCCoast.esm',
                    'DLCworkshop02.esm', 'DLCworkshop03.esm', 'DLCNukaWorld.esm',
                    'DLCUltraHighResolution.esm'),
        'hedr': (0.95, 1.0),
        # Fallout 4 1.10.980+ (HEDR 1.0) reads ESL object IDs 0x001-0xFFF.
        'extended_esl_hedr': 1.0,
        'cc': re.compile(r'^cc[a-z]{2,8}fo4\d{3}', re.I),
    },
}
ALL_MASTERS = {m.casefold(): game for game, spec in GAMES.items() for m in spec['masters']}
ESL_MIN, ESL_MIN_EXTENDED, ESL_MAX = 0x800, 0x001, 0xFFF

CHANGELOG_NAMES = ('CHANGELOG.md', 'CHANGELOG', 'CHANGELOG.txt', 'CHANGES.md', 'docs/CHANGELOG.md')
HEADING = re.compile(r'^\s{0,3}#{1,3}\s+(.*?)\s*#*\s*$')
HEADING_VERSION = re.compile(r'^\[?v?(\d+(?:\.\d+)*(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)\]?(?=$|[\s:(,-])', re.I)
UNRELEASED = re.compile(r'^\[?unreleased\]?', re.I)


class GateError(Exception):
    """Bad input that prevents the gate from running (exit 2)."""


class PluginError(Exception):
    """The file is not a readable TES4-format plugin."""


def finding(check, level, code, subject, detail):
    return {'check': check, 'level': level, 'code': code, 'subject': subject, 'detail': detail}


def split_list(values):
    """Flatten repeatable, comma- or newline-separated option values."""
    out = []
    for value in values or ():
        for item in re.split(r'[\n,]', value):
            item = item.strip()
            if item:
                out.append(item)
    return out


def rel(path, root):
    try:
        return PurePosixPath(Path(path).resolve().relative_to(Path(root).resolve())).as_posix()
    except ValueError:
        return Path(path).as_posix()


def walk(root, skip=SKIP_DIRS):
    """Yield files under root in sorted order, skipping VCS internals and symlinks."""
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d.casefold() not in skip and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            path = Path(dirpath, name)
            if not path.is_symlink():
                yield path


def matches(patterns, relpath):
    """Case-insensitive glob match on the full relative path or the basename."""
    low = relpath.casefold()
    base = PurePosixPath(low).name
    return any(fnmatch.fnmatchcase(low, p.casefold()) or fnmatch.fnmatchcase(base, p.casefold())
               for p in patterns)


# Plugins ---------------------------------------------------------------------

class PluginInfo:
    def __init__(self, path):
        self.path = path
        self.name = PurePosixPath(path).name
        self.flags = 0
        self.form_id = 0
        self.hedr_version = None
        self.num_records = None
        self.next_object_id = None
        self.masters = []
        self.new_ids = []           # object IDs (low 24 bits) of records this plugin adds
        self.overrides = 0
        self.bad_master_index = []  # form IDs whose load-order byte exceeds the master list
        self.records = 0
        self.groups = 0

    @property
    def esl_flag(self):
        return bool(self.flags & FLAG_ESL)

    @property
    def is_light(self):
        """The engines treat a .esl extension as light even without the flag."""
        return self.esl_flag or self.name.casefold().endswith('.esl')

    @property
    def esm_flag(self):
        return bool(self.flags & FLAG_ESM)


def _zstring(raw):
    end = raw.find(b'\0')
    return (raw[:end] if end >= 0 else raw).decode('utf-8', 'replace')


def parse_plugin(data, path='plugin'):
    """Parse the TES4 header and walk record headers (Skyrim SE / Fallout 4 layout)."""
    info = PluginInfo(path)
    if len(data) < 24 or data[:4] != b'TES4':
        raise PluginError('missing TES4 header record')
    size, flags, form_id = struct.unpack_from('<III', data, 4)
    info.flags, info.form_id = flags, form_id
    if 24 + size > len(data):
        raise PluginError('TES4 header (%d bytes) overruns the file' % size)
    if flags & FLAG_COMPRESSED:
        raise PluginError('TES4 header is flagged compressed')
    body = data[24:24 + size]
    pos, override = 0, None
    while pos < len(body):
        if pos + 6 > len(body):
            raise PluginError('truncated subrecord in TES4 header')
        stype = body[pos:pos + 4]
        (ssize,) = struct.unpack_from('<H', body, pos + 4)
        pos += 6
        if stype == b'XXXX':
            if ssize != 4 or pos + 4 > len(body):
                raise PluginError('malformed XXXX subrecord in TES4 header')
            (override,) = struct.unpack_from('<I', body, pos)
            pos += 4
            continue
        if override is not None:
            ssize, override = override, None
        payload = body[pos:pos + ssize]
        if len(payload) != ssize:
            raise PluginError('subrecord %s overruns the TES4 header' % stype.decode('ascii', 'replace'))
        pos += ssize
        if stype == b'HEDR':
            if ssize < 12:
                raise PluginError('HEDR subrecord is %d bytes, expected 12' % ssize)
            version, info.num_records, info.next_object_id = struct.unpack_from('<fiI', payload)
            info.hedr_version = round(version, 2)
        elif stype == b'MAST':
            info.masters.append(_zstring(payload))
    if info.hedr_version is None:
        raise PluginError('TES4 header has no HEDR subrecord')

    # Records and GRUPs are contiguous, so a flat walk visits every record header. A stack of open
    # GRUPs keeps every record and nested group inside the boundary its enclosing group declares.
    pos, total = 24 + size, len(data)
    own_index = len(info.masters)
    groups = []  # (start, end) of each enclosing GRUP, innermost last
    while pos < total:
        while groups and pos == groups[-1][1]:
            groups.pop()
        limit = groups[-1][1] if groups else total
        where = 'the GRUP at offset %d' % groups[-1][0] if groups else 'the file'
        if pos + 24 > limit:
            raise PluginError('truncated record header at offset %d crosses the end of %s' % (pos, where))
        rtype = data[pos:pos + 4]
        (rsize,) = struct.unpack_from('<I', data, pos + 4)
        if rtype == b'GRUP':
            if rsize < 24 or pos + rsize > limit:
                raise PluginError('bad GRUP size %d at offset %d (overruns %s)' % (rsize, pos, where))
            info.groups += 1
            groups.append((pos, pos + rsize))
            pos += 24
            continue
        if not all(65 <= c <= 90 or 48 <= c <= 57 or c == 95 for c in rtype):
            raise PluginError('invalid record type at offset %d' % pos)
        (fid,) = struct.unpack_from('<I', data, pos + 12)
        if pos + 24 + rsize > limit:
            raise PluginError('%s record %08X overruns %s' % (rtype.decode('ascii'), fid, where))
        info.records += 1
        index = fid >> 24
        if index == own_index:
            info.new_ids.append(fid & 0xFFFFFF)
        elif index < own_index:
            info.overrides += 1
        else:
            info.bad_master_index.append(fid)
        pos += 24 + rsize
    return info


def esl_range(game, hedr_version):
    spec = GAMES[game]
    extended = hedr_version is not None and hedr_version >= spec['extended_esl_hedr']
    return (ESL_MIN_EXTENDED if extended else ESL_MIN), ESL_MAX


def discover_plugins(root, patterns):
    root = Path(root)
    if patterns:
        found, missing = [], []
        for pattern in patterns:
            hits = [p for p in sorted(root.glob(pattern)) if p.is_file() and p.suffix.casefold() in PLUGIN_EXTS]
            if not hits and (root / pattern).is_file():
                hits = [root / pattern]
            if not hits:
                missing.append(pattern)
            found.extend(h for h in hits if h not in found)
        return found, missing
    # Stray game masters are the files check's business; do not also parse them as the mod's plugins.
    return [p for p in walk(root, PLUGIN_SKIP_DIRS)
            if p.suffix.casefold() in PLUGIN_EXTS and p.name.casefold() not in ALL_MASTERS], []


def check_plugins(root, game, patterns, esl_policy):
    results, facts = [], []
    plugins, missing = discover_plugins(root, patterns)
    for pattern in missing:
        results.append(finding('plugins', 'error', 'PLUGIN-MISSING', pattern, 'no plugin matches this path or glob'))
    if not plugins and not missing:
        results.append(finding('plugins', 'warning', 'PLUGIN-NONE', '.', 'no .esp/.esm/.esl plugin found'))
    spec = GAMES[game]
    for path in plugins:
        name = rel(path, root)
        try:
            info = parse_plugin(path.read_bytes(), name)
        except PluginError as exc:
            results.append(finding('plugins', 'error', 'PLUGIN-PARSE', name, str(exc)))
            continue
        except OSError as exc:
            results.append(finding('plugins', 'error', 'PLUGIN-READ', name, str(exc)))
            continue
        low, high = esl_range(game, info.hedr_version)
        facts.append({'plugin': name, 'flags': '0x%X' % info.flags, 'esl': info.is_light, 'esm': info.esm_flag,
                      'hedr': info.hedr_version, 'masters': info.masters, 'new_records': len(info.new_ids),
                      'overrides': info.overrides, 'esl_range': '0x%03X-0x%03X' % (low, high)})
        if info.hedr_version not in spec['hedr']:
            other = [g for g, s in GAMES.items() if g != game and info.hedr_version in s['hedr']]
            hint = ' (looks like a %s plugin)' % GAMES[other[0]]['title'] if other else ''
            results.append(finding('plugins', 'warning', 'PLUGIN-HEDR', name,
                                   'HEDR version %s, expected %s for %s%s' % (
                                       info.hedr_version, ' or '.join(str(v) for v in spec['hedr']),
                                       spec['title'], hint)))
        for master in info.masters:
            owner = ALL_MASTERS.get(master.casefold())
            if owner and owner != game:
                results.append(finding('plugins', 'error', 'PLUGIN-WRONG-GAME', name,
                                       'requires %s, a %s master' % (master, GAMES[owner]['title'])))
            if master.casefold() == info.name.casefold():
                results.append(finding('plugins', 'error', 'PLUGIN-SELF-MASTER', name, 'lists itself as a master'))
        if info.bad_master_index:
            results.append(finding('plugins', 'error', 'PLUGIN-FORMID', name,
                                   '%d record(s) use a load-order index beyond the %d master(s), first %08X'
                                   % (len(info.bad_master_index), len(info.masters), info.bad_master_index[0])))
        if name.casefold().endswith('.esm') and not info.esm_flag:
            results.append(finding('plugins', 'warning', 'PLUGIN-ESM-FLAG', name,
                                   '.esm extension without the ESM flag (0x1)'))
        if name.casefold().endswith('.esl') and not info.esl_flag:
            results.append(finding('plugins', 'warning', 'ESL-EXT-NOFLAG', name,
                                   '.esl extension without the ESL flag (0x200); the game loads it as light anyway'))
        if esl_policy == 'require' and not info.is_light:
            results.append(finding('plugins', 'error', 'ESL-REQUIRED', name,
                                   'ESL flag 0x200 is not set (TES4 flags 0x%X)' % info.flags))
        elif esl_policy == 'forbid' and info.is_light:
            results.append(finding('plugins', 'error', 'ESL-FORBIDDEN', name,
                                   'plugin is light (flags 0x%X) but esl policy is forbid' % info.flags))
        if info.is_light:
            outside = [i for i in info.new_ids if not low <= i <= high]
            if outside:
                results.append(finding('plugins', 'error', 'ESL-RANGE', name,
                                       '%d new record(s) outside 0x%03X-0x%03X, e.g. %s' % (
                                           len(outside), low, high,
                                           ', '.join('%06X' % i for i in sorted(outside)[:5]))))
            capacity = high - low + 1
            if len(info.new_ids) > capacity:
                results.append(finding('plugins', 'error', 'ESL-BUDGET', name,
                                       '%d new records; a light plugin holds at most %d' % (len(info.new_ids), capacity)))
            if info.next_object_id is not None and info.next_object_id > high + 1:
                results.append(finding('plugins', 'warning', 'ESL-NEXT-ID', name,
                                       'HEDR next object ID 0x%X is beyond the light range' % info.next_object_id))
            if not outside and len(info.new_ids) <= capacity:
                results.append(finding('plugins', 'notice', 'ESL-OK', name,
                                       'light plugin, %d/%d new record IDs used' % (len(info.new_ids), capacity)))
    return results, facts


# Version ---------------------------------------------------------------------

def normalize_version(text):
    """'v1.2.0' -> '1.2'; trailing .0 components in the numeric part are not significant."""
    text = text.strip()
    if text[:1] in 'vV':
        text = text[1:]
    text = text.split('+', 1)[0]
    core, sep, pre = text.partition('-')
    parts = core.split('.')
    while len(parts) > 1 and parts[-1].isdigit() and int(parts[-1]) == 0:
        parts.pop()
    parts = [str(int(p)) if p.isdigit() else p for p in parts]
    return '.'.join(parts) + (sep + pre.casefold() if sep else '')


def changelog_top(text):
    """Return (version, heading) of the first released changelog entry, or (None, None)."""
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            in_fence = not in_fence
            continue
        match = None if in_fence else HEADING.match(line)
        if not match:
            continue
        heading = match.group(1).strip()
        if UNRELEASED.match(heading):
            continue
        version = HEADING_VERSION.match(heading)
        if version:
            return version.group(1), heading
        # Allow 'Version 1.2.3' and 'MyMod 1.2.3 - date' styles.
        loose = re.search(r'(?:^|\s)v?(\d+\.\d+(?:\.\d+)*(?:-[0-9A-Za-z.]+)?)\b', heading)
        if loose:
            return loose.group(1), heading
    return None, None


def find_changelog(root, given):
    if given:
        path = Path(root) / given
        return path if path.is_file() else None, True
    for name in CHANGELOG_NAMES:
        path = Path(root) / name
        if path.is_file():
            return path, False
    return None, False


def git_tag(root):
    """Read-only: the tag pointing at HEAD, if any."""
    try:
        out = subprocess.run(['git', '-C', str(root), 'describe', '--tags', '--exact-match', 'HEAD'],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    tag = out.stdout.decode('utf-8', 'replace').strip()
    return tag if out.returncode == 0 and tag else None


def resolve_tag(root, tag, use_git):
    if tag:
        return tag, 'input'
    if os.environ.get('GITHUB_REF_TYPE') == 'tag' and os.environ.get('GITHUB_REF_NAME'):
        return os.environ['GITHUB_REF_NAME'], 'GITHUB_REF_NAME'
    if use_git:
        found = git_tag(root)
        if found:
            return found, 'git describe'
    return None, None


def check_version(root, tag, tag_prefix, changelog, fomod_dir, use_git, require_tag):
    results, versions = [], []
    tag, origin = resolve_tag(root, tag, use_git)
    if tag:
        stripped = tag[len(tag_prefix):] if tag_prefix and tag.startswith(tag_prefix) else tag
        if tag_prefix and not tag.startswith(tag_prefix):
            results.append(finding('version', 'warning', 'VER-TAG-PREFIX', tag,
                                   'tag does not start with the expected prefix %r' % tag_prefix))
        if not re.match(r'^v?\d+(\.\d+)*', stripped):
            results.append(finding('version', 'error', 'VER-TAG-FORMAT', tag, 'tag does not contain a version number'))
        versions.append({'source': 'git tag (%s)' % origin, 'raw': tag, 'version': stripped})
    else:
        level = 'error' if require_tag else 'notice'
        results.append(finding('version', level, 'VER-NO-TAG', '-',
                               'no release tag (input, GITHUB_REF_NAME or tag at HEAD); tag comparison skipped'))

    path, explicit = find_changelog(root, changelog)
    if path is None:
        level = 'error' if explicit or tag else 'warning'
        results.append(finding('version', level, 'VER-CHANGELOG-MISSING', changelog or 'CHANGELOG.md',
                               'changelog not found'))
    else:
        text = path.read_text(encoding='utf-8', errors='replace')
        top, heading = changelog_top(text)
        if top is None:
            results.append(finding('version', 'error', 'VER-CHANGELOG-ENTRY', rel(path, root),
                                   'no versioned heading (e.g. "## [1.2.0] - 2026-01-31") found'))
        else:
            versions.append({'source': 'changelog top entry', 'raw': heading, 'version': top})

    if fomod_dir is not None:
        info_xml = find_ci(fomod_dir, 'info.xml')
        if info_xml is not None:
            try:
                version = ET.fromstring(info_xml.read_bytes()).findtext('Version')
            except ET.ParseError:
                version = None  # reported by the fomod check
            if version and version.strip():
                versions.append({'source': 'fomod info.xml', 'raw': version.strip(), 'version': version.strip()})

    if len(versions) > 1:
        reference = versions[0]
        for other in versions[1:]:
            if normalize_version(other['version']) != normalize_version(reference['version']):
                results.append(finding('version', 'error', 'VER-MISMATCH', other['source'],
                                       '%s is %s but %s is %s' % (other['source'], other['version'],
                                                                   reference['source'], reference['version'])))
        if not any(r['code'] == 'VER-MISMATCH' for r in results):
            results.append(finding('version', 'notice', 'VER-OK', reference['version'],
                                   'agrees across %s' % ', '.join(v['source'] for v in versions)))
    return results, versions


# Stray files -------------------------------------------------------------------

def file_rules(game, extra_forbid):
    """(code, description, predicate(relpath)) tuples; masters of both games are always forbidden."""
    cc = [spec['cc'] for spec in GAMES.values()]

    def base(p):
        return PurePosixPath(p.casefold()).name

    rules = [
        ('FILE-GAME-MASTER', 'Bethesda master file', lambda p: base(p) in ALL_MASTERS),
        ('FILE-CREATION-CLUB', 'Creation Club content',
         lambda p: base(p).endswith(('.esm', '.esl', '.esp', '.bsa', '.ba2')) and any(r.match(base(p)) for r in cc)),
        ('FILE-ARCHIVE', 'Bethesda archive (.bsa/.ba2)', lambda p: base(p).endswith(('.bsa', '.ba2'))),
        ('FILE-PAPYRUS-SOURCE', 'Papyrus source (.psc)', lambda p: base(p).endswith('.psc')),
    ]
    if extra_forbid:
        rules.append(('FILE-FORBIDDEN', 'matches a forbid pattern', lambda p: matches(extra_forbid, p)))
    return rules


def list_scan_target(root, target):
    """Yield (display path, relative path) for a directory tree or a zip archive."""
    path = Path(target)
    if not path.is_absolute():
        path = Path(root) / path
    if path.is_dir():
        for item in walk(path):
            yield rel(item, root), rel(item, path)
    elif path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    name = member.filename.replace('\\', '/')
                    yield '%s!/%s' % (rel(path, root), name), name
    else:
        raise GateError('scan path is not a directory or zip: %s' % target)


def check_files(root, game, scan, allow, forbid):
    results, scanned = [], 0
    rules = file_rules(game, forbid)
    for target in scan or ['.']:
        for display, relpath in list_scan_target(root, target):
            scanned += 1
            hits = [(code, label) for code, label, test in rules if test(relpath)]
            if not hits:
                continue
            code, label = hits[0]
            if matches(allow, relpath) or matches(allow, display):
                results.append(finding('files', 'notice', 'FILE-ALLOWED', display, '%s, allowlisted' % label))
            else:
                results.append(finding('files', 'error', code, display, label))
    if not any(r['level'] == 'error' for r in results):
        results.append(finding('files', 'notice', 'FILE-OK', '.', '%d file(s) scanned, none forbidden' % scanned))
    return results


# FOMOD -------------------------------------------------------------------------

def find_ci(directory, name):
    directory = Path(directory)
    if not directory.is_dir():
        return None
    for child in sorted(directory.iterdir()):
        if child.name.casefold() == name.casefold() and child.is_file():
            return child
    return None


def find_fomod(root, given):
    """Return (fomod dir or None, explicit)."""
    root = Path(root)
    if given:
        path = root / given
        if path.is_file():
            path = path.parent
        return (path if path.is_dir() else None), True
    for path in walk(root):
        if path.name.casefold() == 'moduleconfig.xml' and path.parent.name.casefold() == 'fomod':
            return path.parent, False
    return None, False


def _strip_ns(root):
    for node in root.iter():
        if isinstance(node.tag, str):
            node.tag = node.tag.split('}')[-1]
    return root


def _inside(path, base):
    """True if path, with every symlink resolved, stays within base (also resolved)."""
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _escaping_links(folder, base):
    """Entries under folder that are symlinks resolving outside base.

    Directory symlinks that stay inside base are followed too (with cycle detection on the
    resolved path), so a chain of internal aliases cannot hide an external link."""
    found, seen, stack = [], set(), [Path(folder)]
    while stack:
        current = stack.pop()
        try:
            key = current.resolve()
        except (OSError, RuntimeError):
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                if not _inside(entry, base):
                    found.append(entry)
                    continue
                if entry.is_dir():
                    stack.append(entry)
            elif entry.is_dir():
                stack.append(entry)
    return found


def _exists_ci(base, relpath):
    """Case-insensitive lookup the way mod managers on Windows resolve FOMOD sources.

    Symlinks are followed, so callers must check the hit with _inside() before trusting it.
    """
    current = Path(base)
    for part in PurePosixPath(relpath).parts:
        if not current.is_dir():
            return None
        exact = current / part
        if exact.exists():
            current = exact
            continue
        hit = [c for c in current.iterdir() if c.name.casefold() == part.casefold()]
        if not hit:
            return None
        current = hit[0]
    return current


def check_fomod(root, given):
    results = []
    fomod_dir, explicit = find_fomod(root, given)
    if fomod_dir is None:
        if explicit:
            results.append(finding('fomod', 'error', 'FOMOD-MISSING', given, 'FOMOD directory not found'))
        else:
            results.append(finding('fomod', 'notice', 'FOMOD-NONE', '-', 'no fomod/ModuleConfig.xml; check skipped'))
        return results, None
    package = fomod_dir.parent
    shown = rel(fomod_dir, root)

    info = find_ci(fomod_dir, 'info.xml')
    if info is None:
        results.append(finding('fomod', 'warning', 'FOMOD-INFO-MISSING', shown + '/info.xml', 'info.xml not found'))
    else:
        try:
            tree = _strip_ns(ET.fromstring(info.read_bytes()))
            if tree.tag.casefold() != 'fomod':
                results.append(finding('fomod', 'warning', 'FOMOD-INFO-ROOT', rel(info, root),
                                       'root element is <%s>, expected <fomod>' % tree.tag))
            if not (tree.findtext('Name') or '').strip():
                results.append(finding('fomod', 'warning', 'FOMOD-INFO-NAME', rel(info, root), '<Name> is empty or missing'))
        except ET.ParseError as exc:
            results.append(finding('fomod', 'error', 'FOMOD-XML', rel(info, root), 'not well-formed: %s' % exc))

    config = find_ci(fomod_dir, 'ModuleConfig.xml')
    if config is None:
        results.append(finding('fomod', 'error', 'FOMOD-CONFIG-MISSING', shown + '/ModuleConfig.xml',
                               'ModuleConfig.xml not found'))
        return results, fomod_dir
    try:
        tree = _strip_ns(ET.fromstring(config.read_bytes()))
    except ET.ParseError as exc:
        results.append(finding('fomod', 'error', 'FOMOD-XML', rel(config, root), 'not well-formed: %s' % exc))
        return results, fomod_dir
    if tree.tag != 'config':
        results.append(finding('fomod', 'error', 'FOMOD-CONFIG-ROOT', rel(config, root),
                               'root element is <%s>, expected <config>' % tree.tag))
    if not (tree.findtext('moduleName') or '').strip():
        results.append(finding('fomod', 'warning', 'FOMOD-MODULE-NAME', rel(config, root), '<moduleName> is empty or missing'))

    refs = []
    for node in tree.iter():
        if node.tag in ('file', 'folder'):
            if not (node.get('source') or '').strip():
                subject = '<%s destination="%s">' % (node.tag, node.get('destination') or '')
                results.append(finding('fomod', 'error', 'FOMOD-NO-SOURCE', subject,
                                       '<%s> has no source attribute' % node.tag))
                continue
            refs.append((node.tag, node.get('source')))
        elif node.tag in ('moduleImage', 'image') and node.get('path'):
            refs.append(('image', node.get('path')))
    checked, missing = 0, 0
    for kind, source in refs:
        cleaned = source.replace('\\', '/').strip()
        posix = PurePosixPath(cleaned)
        if not cleaned or posix.is_absolute() or re.match(r'^[A-Za-z]:', cleaned) or '..' in posix.parts:
            results.append(finding('fomod', 'error', 'FOMOD-BAD-PATH', source,
                                   '%s source must be a relative path inside the package' % kind))
            continue
        checked += 1
        hit = _exists_ci(package, cleaned)
        if hit is not None and not _inside(hit, package):
            results.append(finding('fomod', 'error', 'FOMOD-SYMLINK-ESCAPE', cleaned,
                                   '%s resolves outside the package via a symlink' % kind))
            continue
        escaping = _escaping_links(hit, package) if hit is not None and kind == 'folder' and hit.is_dir() else []
        if escaping:
            results.append(finding('fomod', 'error', 'FOMOD-SYMLINK-ESCAPE', cleaned,
                                   'folder contains %d symlink(s) resolving outside the package, first %s'
                                   % (len(escaping), (PurePosixPath(cleaned) / escaping[0].relative_to(hit).as_posix()))))
            continue
        if hit is None or (kind == 'folder' and not hit.is_dir()) or (kind != 'folder' and not hit.is_file()):
            missing += 1
            results.append(finding('fomod', 'error', 'FOMOD-MISSING-SOURCE', cleaned,
                                   '%s referenced by ModuleConfig.xml does not exist under %s'
                                   % (kind, rel(package, root) or '.')))
        elif kind == 'folder' and not any(p.is_file() for p in hit.rglob('*')):
            results.append(finding('fomod', 'warning', 'FOMOD-EMPTY-FOLDER', cleaned, 'referenced folder has no files'))
    if not any(r['level'] == 'error' for r in results):
        results.append(finding('fomod', 'notice', 'FOMOD-OK', shown,
                               'well-formed; %d referenced path(s) exist' % checked))
    return results, fomod_dir


# Conflicts (mod-conflict-graph) ------------------------------------------------

def load_modconflict(mcg_path):
    """Import mod-conflict-graph's modcheck module, from a checkout path if given."""
    if mcg_path:
        candidate = Path(mcg_path).resolve()
        if (candidate / 'modconflict').is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    try:
        from modconflict import modcheck  # noqa: WPS433 (optional dependency)
        return modcheck, None
    except Exception as exc:  # any import failure degrades to a notice
        return None, '%s: %s' % (type(exc).__name__, exc)


def check_conflicts(root, game, against, mcg_path, fail_on):
    modcheck, problem = load_modconflict(mcg_path)
    if modcheck is None:
        return [finding('conflicts', 'notice', 'CONFLICTS-UNAVAILABLE', 'mod-conflict-graph',
                        'not importable (%s); conflict report skipped' % problem)], []
    try:
        config = modcheck.load_config(None)
        config['conflicts_fail_on'] = fail_on
        config['implicit'] = list(GAMES[game]['masters'])
        for path in against:
            if not (Path(root) / path).is_dir():
                raise GateError('--against is not a directory: %s' % path)
        result = modcheck.run(Path(root), config, against=[Path(root) / a for a in against], use_git=False,
                              fail_on='never')
    except GateError:
        raise
    except Exception as exc:  # the engine is optional; never let it crash the gate
        return [finding('conflicts', 'warning', 'CONFLICTS-ERROR', 'mod-conflict-graph',
                        'analysis failed: %s: %s' % (type(exc).__name__, exc))], []
    results = []
    level_map = {'blocker': 'error', 'warning': 'warning', 'info': 'notice'}
    for item in result['findings']:
        if item['check'] not in ('conflicts', 'masters'):
            continue
        level = level_map.get(item['level'], 'notice')
        if item['check'] == 'masters' and level == 'error':
            level = 'warning'   # undeclared masters are advisory here; the plugins check owns hard errors
        code = 'CONFLICT-' + item['kind'].upper() if item['check'] == 'conflicts' else 'MASTER-' + item['kind'].upper()
        results.append(finding('conflicts', level, code, item['subject'], item['detail']))
    notes = []
    if game != 'skyrimse':
        notes.append('mod-conflict-graph is tuned for Skyrim SE; %s results are best-effort'
                     % GAMES[game]['title'])
    if not results:
        results.append(finding('conflicts', 'notice', 'CONFLICTS-OK', '.',
                               'no conflicts reported%s' % (' against %d mod(s)' % len(against) if against else '')))
    for note in notes:
        results.append(finding('conflicts', 'notice', 'CONFLICTS-NOTE', '-', note))
    return results, result.get('load_order') or []


# Report --------------------------------------------------------------------------

def md_escape(text):
    return str(text).replace('\\', '\\\\').replace('|', '\\|').replace('\n', ' ').replace('<', '&lt;')


def render(result):
    icon = {'pass': 'PASS', 'fail': 'FAIL', 'skipped': 'skipped'}
    counts = {lvl: sum(f['level'] == lvl for f in result['findings']) for lvl in LEVELS}
    lines = ['## mod-release-gate: %s (%s)' % ('PASS' if result['result'] == 'pass' else 'FAIL',
                                                GAMES[result['game']]['title']), '',
             '%d error(s), %d warning(s), %d notice(s). Fails on: `%s`.' % (
                 counts['error'], counts['warning'], counts['notice'], result['fail_on']), '',
             '| Check | Status | Errors | Warnings |', '| --- | --- | ---: | ---: |']
    for check in CHECKS:
        items = [f for f in result['findings'] if f['check'] == check]
        lines.append('| %s | %s | %d | %d |' % (check, icon[result['checks'][check]],
                                               sum(f['level'] == 'error' for f in items),
                                               sum(f['level'] == 'warning' for f in items)))
    for level, title in (('error', 'Errors'), ('warning', 'Warnings')):
        items = [f for f in result['findings'] if f['level'] == level]
        if items:
            lines += ['', '### ' + title, '', '| Check | Code | Subject | Detail |', '| --- | --- | --- | --- |']
            lines += ['| %s | `%s` | %s | %s |' % (f['check'], f['code'], md_escape(f['subject']), md_escape(f['detail']))
                      for f in items]
    if result['plugins']:
        lines += ['', '### Plugins', '', '| Plugin | Flags | Light | HEDR | New records | Overrides | Masters |',
                  '| --- | --- | --- | --- | ---: | ---: | --- |']
        for p in result['plugins']:
            lines.append('| %s | %s | %s | %s | %d | %d | %s |' % (
                md_escape(p['plugin']), p['flags'], 'yes (%s)' % p['esl_range'] if p['esl'] else 'no', p['hedr'],
                p['new_records'], p['overrides'], md_escape(', '.join(p['masters']) or '-')))
    if result['versions']:
        lines += ['', '### Versions', '', '| Source | Value | Version |', '| --- | --- | --- |']
        lines += ['| %s | %s | %s |' % (md_escape(v['source']), md_escape(v['raw']), md_escape(v['version']))
                  for v in result['versions']]
    if result['load_order']:
        lines += ['', '### Suggested load order', '', ' → '.join('`%s`' % md_escape(p) for p in result['load_order'])]
    notices = [f for f in result['findings'] if f['level'] == 'notice']
    if notices:
        lines += ['', '<details><summary>Notices (%d)</summary>' % len(notices), '',
                  '| Check | Code | Subject | Detail |', '| --- | --- | --- | --- |']
        lines += ['| %s | `%s` | %s | %s |' % (f['check'], f['code'], md_escape(f['subject']), md_escape(f['detail']))
                  for f in notices]
        lines += ['', '</details>']
    return '\n'.join(lines) + '\n'


def run(args):
    root = Path(args.root)
    if not root.is_dir():
        raise GateError('mod directory does not exist: %s' % args.root)
    skip = set(split_list(args.skip))
    unknown = skip - set(CHECKS)
    advisory = set(split_list(args.advisory))
    unknown |= advisory - set(CHECKS)
    if unknown:
        raise GateError('unknown check(s): %s (choose from %s)' % (', '.join(sorted(unknown)), ', '.join(CHECKS)))

    findings, plugins, versions, order = [], [], [], []
    fomod_dir = None
    if 'fomod' not in skip:
        items, fomod_dir = check_fomod(root, args.fomod)
        findings += items
    else:
        fomod_dir = find_fomod(root, args.fomod)[0]
    if 'plugins' not in skip:
        items, plugins = check_plugins(root, args.game, split_list(args.plugins), args.esl)
        findings += items
    if 'version' not in skip:
        items, versions = check_version(root, args.tag, args.tag_prefix, args.changelog, fomod_dir,
                                        not args.no_git, args.require_tag)
        findings += items
    if 'files' not in skip:
        findings += check_files(root, args.game, split_list(args.scan), split_list(args.allow), split_list(args.forbid))
    if 'conflicts' not in skip:
        items, order = check_conflicts(root, args.game, split_list(args.against), args.mod_conflict_graph,
                                       args.conflicts_fail_on)
        if args.require_conflicts and any(f['code'] == 'CONFLICTS-UNAVAILABLE' for f in items):
            for item in items:
                item['level'] = 'error'
        findings += items

    for item in findings:
        if item['check'] in advisory and item['level'] == 'error':
            item['level'] = 'warning'
            item['detail'] += ' (advisory)'
    findings.sort(key=lambda f: (LEVELS.index(f['level']), CHECKS.index(f['check']), f['code'], f['subject']))

    failing = {'error': ('error',), 'warning': ('error', 'warning'), 'never': ()}[args.fail_on]
    checks = {}
    for check in CHECKS:
        if check in skip:
            checks[check] = 'skipped'
        else:
            bad = any(f['check'] == check and f['level'] in failing for f in findings)
            checks[check] = 'fail' if bad else 'pass'
    failed = any(f['level'] in failing for f in findings)
    return {'tool': 'mod-release-gate', 'version': __version__, 'game': args.game, 'fail_on': args.fail_on,
            'result': 'fail' if failed else 'pass', 'checks': checks, 'findings': findings,
            'plugins': plugins, 'versions': versions, 'load_order': order}


def parser():
    p = argparse.ArgumentParser(prog='mod_release_gate.py',
                                description='Pre-release gate for Skyrim SE and Fallout 4 mod repositories.')
    p.add_argument('--root', default='.', help='Mod directory (default: .)')
    p.add_argument('--game', required=True, choices=sorted(GAMES), help='Target game')
    p.add_argument('--plugins', action='append', default=[], metavar='GLOB',
                   help='Plugin paths/globs relative to --root (repeatable, comma/newline separated; default: discover)')
    p.add_argument('--esl', choices=('any', 'require', 'forbid'), default='any',
                   help='ESL policy for every checked plugin (default: any)')
    p.add_argument('--tag', default='', help='Release tag (default: GITHUB_REF_NAME on tag events, else tag at HEAD)')
    p.add_argument('--tag-prefix', default='v', help="Prefix stripped from the tag (default: 'v')")
    p.add_argument('--require-tag', action='store_true', help='Fail when no release tag can be determined')
    p.add_argument('--no-git', action='store_true', help='Never call git')
    p.add_argument('--changelog', default='', help='Changelog path relative to --root (default: discover)')
    p.add_argument('--fomod', default='', help='FOMOD directory relative to --root (default: discover)')
    p.add_argument('--scan', action='append', default=[], metavar='PATH',
                   help='Directory or zip to scan for forbidden files (repeatable; default: --root)')
    p.add_argument('--allow', action='append', default=[], metavar='GLOB', help='Allowlist globs for the files check')
    p.add_argument('--forbid', action='append', default=[], metavar='GLOB', help='Extra forbidden globs')
    p.add_argument('--against', action='append', default=[], metavar='DIR',
                   help='Other mod directories for the conflict report (repeatable)')
    p.add_argument('--mod-conflict-graph', default=os.environ.get('MOD_CONFLICT_GRAPH_PATH', ''), metavar='DIR',
                   help='Path to a mod-conflict-graph checkout (default: import from sys.path)')
    p.add_argument('--conflicts-fail-on', choices=('collision', 'probable', 'warning', 'never'), default='collision',
                   help='mod-conflict-graph severity that counts as an error (default: collision)')
    p.add_argument('--require-conflicts', action='store_true', help='Fail when mod-conflict-graph is unavailable')
    p.add_argument('--skip', action='append', default=[], metavar='CHECKS', help='Checks to skip: ' + ', '.join(CHECKS))
    p.add_argument('--advisory', action='append', default=[], metavar='CHECKS',
                   help='Checks whose errors are downgraded to warnings')
    p.add_argument('--fail-on', choices=('error', 'warning', 'never'), default='error',
                   help='Lowest level that fails the gate (default: error)')
    p.add_argument('--summary', default='', help='Append the Markdown report here (e.g. $GITHUB_STEP_SUMMARY)')
    p.add_argument('--report', default='', help='Write the Markdown report to this file')
    p.add_argument('--json', default='', help='Write the JSON result to this file')
    p.add_argument('--github-output', default='', help='Append result=pass|fail etc. to this file ($GITHUB_OUTPUT)')
    p.add_argument('--quiet', action='store_true', help='Do not print the report to stdout')
    p.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    return p


def annotate(findings):
    for item in findings:
        if item['level'] in ('error', 'warning'):
            message = '%s %s: %s' % (item['code'], item['subject'], item['detail'])
            message = message.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
            print('::%s title=mod-release-gate %s::%s' % (item['level'], item['check'], message))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
    except GateError as exc:
        print('mod-release-gate: %s' % exc, file=sys.stderr)
        if args.github_output:
            with open(args.github_output, 'a', encoding='utf-8') as handle:
                handle.write('result=fail\n')
        return 2
    text = render(result)
    if not args.quiet:
        sys.stdout.write(text)
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        annotate(result['findings'])
    for target, mode, content in ((args.summary, 'a', text), (args.report, 'w', text),
                                  (args.json, 'w', json.dumps(result, indent=2, sort_keys=True) + '\n')):
        if target:
            with open(target, mode, encoding='utf-8') as handle:
                handle.write(content)
    if args.github_output:
        counts = {lvl: sum(f['level'] == lvl for f in result['findings']) for lvl in LEVELS}
        with open(args.github_output, 'a', encoding='utf-8') as handle:
            handle.write('result=%s\nerrors=%d\nwarnings=%d\n' % (result['result'], counts['error'], counts['warning']))
    return 1 if result['result'] == 'fail' else 0


if __name__ == '__main__':
    sys.exit(main())
