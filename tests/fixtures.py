"""Synthetic mod fixtures. Plugin bytes are generated here; no game data is used."""
import struct
from pathlib import Path

FLAG_ESM = 0x1
FLAG_ESL = 0x200


def subrecord(stype, payload):
    return stype + struct.pack('<H', len(payload)) + payload


def record(rtype, form_id, body=b'', flags=0):
    return rtype + struct.pack('<IIIIHH', len(body), flags, form_id, 0, 44, 0) + body


def grup(label, contents, group_type=0):
    return b'GRUP' + struct.pack('<I4siIHH', 24 + len(contents), label, group_type, 0, 44, 0) + contents


def plugin_bytes(masters=(), flags=0, hedr=1.71, new_ids=(), overrides=(), next_id=None, record_type=b'MISC',
                 author='synthalorian', raw_form_ids=()):
    """Build a minimal TES4 plugin.

    new_ids are object IDs created by this plugin (load-order byte = number of masters);
    overrides are full form IDs owned by a master; raw_form_ids are stored verbatim.
    """
    own = len(masters)
    ids = [(own << 24) | i for i in new_ids] + list(overrides) + list(raw_form_ids)
    count = len(ids) + (1 if ids else 0)
    if next_id is None:
        next_id = max(list(new_ids) + [0x7FF]) + 1
    header = subrecord(b'HEDR', struct.pack('<fiI', hedr, count, next_id))
    header += subrecord(b'CNAM', author.encode() + b'\0')
    for master in masters:
        header += subrecord(b'MAST', master.encode() + b'\0') + subrecord(b'DATA', b'\0' * 8)
    data = record(b'TES4', 0, header, flags)
    if ids:
        body = b''.join(record(record_type, fid, subrecord(b'EDID', b'Test%06X\0' % (fid & 0xFFFFFF)))
                        for fid in ids)
        data += grup(record_type, body)
    return data


def write(root, relpath, content):
    path = Path(root) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode('utf-8')
    path.write_bytes(content)
    return path


INFO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<fomod>
  <Name>Test Mod</Name>
  <Author>synthalorian</Author>
  <Version>{version}</Version>
</fomod>
"""

MODULE_CONFIG = """<?xml version="1.0" encoding="UTF-8"?>
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:noNamespaceSchemaLocation="http://qconsulting.ca/fo3/ModConfig5.0.xsd">
  <moduleName>Test Mod</moduleName>
  <moduleImage path="fomod\\images\\banner.png"/>
  <requiredInstallFiles>
    <file source="{plugin}" destination="{plugin}"/>
  </requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Options">
      <optionalFileGroups order="Explicit">
        <group name="Extras" type="SelectAny">
          <plugins order="Explicit">
            <plugin name="Textures">
              <description>Optional textures.</description>
              <image path="fomod/images/textures.png"/>
              <files><folder source="optional\\textures" destination="textures"/></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
</config>
"""


def make_mod(root, game='skyrimse', version='1.2.0', esl=True, plugin='TestMod.esp', changelog=True, fomod=True):
    """A clean mod tree that passes every check."""
    root = Path(root)
    master = 'Skyrim.esm' if game == 'skyrimse' else 'Fallout4.esm'
    hedr = 1.71 if game == 'skyrimse' else 1.0
    write(root, plugin, plugin_bytes(masters=[master], flags=FLAG_ESL if esl else 0, hedr=hedr,
                                     new_ids=[0x800, 0x801, 0x802], overrides=[0x00012E46]))
    if changelog:
        write(root, 'CHANGELOG.md', '# Changelog\n\n## [Unreleased]\n\n- wip\n\n'
                                    '## [%s] - 2026-10-30\n\n- Added things.\n\n## [1.0.0] - 2026-01-01\n\n- First.\n'
              % version)
    if fomod:
        write(root, 'fomod/info.xml', INFO_XML.format(version=version))
        write(root, 'fomod/ModuleConfig.xml', MODULE_CONFIG.format(plugin=plugin))
        write(root, 'fomod/images/banner.png', b'\x89PNG')
        write(root, 'fomod/images/textures.png', b'\x89PNG')
        write(root, 'optional/textures/a.dds', b'DDS ')
    write(root, 'README.md', '# Test Mod\n\nRequires %s.\n' % master)
    return root
