# mod-release-gate

A reusable composite GitHub Action that runs pre-release checks on a **Skyrim Special Edition** or **Fallout 4** mod repository. It fails the job before a broken or non-compliant release is tagged or uploaded.

The checks are written in one standard-library Python script, [`scripts/mod_release_gate.py`](scripts/mod_release_gate.py) (Python 3.8+). The action wraps that script, so you can run the same checks locally. The script reads your files and never modifies them. It runs git only for a read-only `git describe`, and only when you give it no tag.

## Checks

| Check | What it verifies | Typical codes |
| --- | --- | --- |
| `plugins` | Parses the TES4 header of each `.esp`/`.esm`/`.esl` and walks every record header. Applies the ESL policy (`require`, `forbid`, or `any`). For light plugins, checks that new form IDs fall inside the light range and that the record budget is not exceeded. Also checks the HEDR version for the selected game, master indexes that point past the master list, masters from the wrong game, and a plugin that lists itself as a master. | `ESL-REQUIRED` `ESL-FORBIDDEN` `ESL-RANGE` `ESL-BUDGET` `ESL-NEXT-ID` `PLUGIN-HEDR` `PLUGIN-FORMID` `PLUGIN-WRONG-GAME` `PLUGIN-PARSE` |
| `version` | Requires the git tag (minus `tag-prefix`), the top released CHANGELOG entry, and the FOMOD `info.xml` `<Version>` (when present) to agree. `[Unreleased]` headings and fenced code are skipped. Trailing `.0` parts are ignored, so `v1.2` equals `1.2.0`. | `VER-MISMATCH` `VER-CHANGELOG-MISSING` `VER-CHANGELOG-ENTRY` `VER-NO-TAG` |
| `files` | Rejects Bethesda master files for either game (base game, DLC, `_ResourcePack.esl`), Creation Club content, `.bsa`/`.ba2` archives, `.psc` Papyrus sources, and any extra `forbid` globs, unless they match `allow`. Scans directories or zip files. | `FILE-GAME-MASTER` `FILE-CREATION-CLUB` `FILE-ARCHIVE` `FILE-PAPYRUS-SOURCE` `FILE-FORBIDDEN` |
| `fomod` | Checks that `fomod/info.xml` and `fomod/ModuleConfig.xml` are well-formed (UTF-8 and UTF-16 are both accepted). Every `<file>`, `<folder>`, `<moduleImage>` and `<image>` they reference must exist; paths are compared case-insensitively and `\` separators are accepted. Absolute paths and `..` are rejected. | `FOMOD-XML` `FOMOD-MISSING-SOURCE` `FOMOD-BAD-PATH` `FOMOD-CONFIG-MISSING` |
| `conflicts` | Runs [mod-conflict-graph](https://github.com/Blackshield-Company/mod-conflict-graph) as a library: record overrides, EditorID and script collisions, master declarations, and a suggested load order against the `against` mods. If it cannot be loaded, the check is skipped with a notice. | `CONFLICT-*` `MASTER-*` `CONFLICTS-UNAVAILABLE` |

### ESL rules

| Game | HEDR | Light new-record object IDs | Capacity |
| --- | --- | --- | --- |
| Skyrim SE | 1.70 | `0x800`–`0xFFF` | 2048 |
| Skyrim SE (1.6.1130+) | 1.71 | `0x001`–`0xFFF` | 4095 |
| Fallout 4 | 0.95 | `0x800`–`0xFFF` | 2048 |
| Fallout 4 (1.10.980+) | 1.0 | `0x001`–`0xFFF` | 4095 |

The engines treat a plugin as light if it has the ESL flag (`0x200`) **or** a `.esl` extension. Only records the plugin creates count toward the limit, meaning form IDs whose load-order byte equals the number of masters. Overrides of master records do not count.

### Levels

Each finding is an `error`, `warning`, or `notice`. `fail-on` sets the lowest level that fails the gate: `error` (default), `warning`, or `never`. Listing a check in `advisory` turns its errors into warnings. Listing a check in `skip` disables it.

## Usage

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- id: gate
  uses: synthalorian/mod-release-gate@v1   # pin to your copy / a commit SHA
  with:
    game: skyrimse
    plugins: MyMod.esp
    esl: require
    allow: |
      Source/Scripts/*.psc
```

See [`examples/workflow.yml`](examples/workflow.yml) for a complete workflow.

### Inputs

| Input | Default | Description |
| --- | --- | --- |
| `game` | (required) | `skyrimse` or `fallout4` |
| `path` | `.` | Mod directory |
| `plugins` | discover | Plugin paths/globs, one per line or comma separated. Discovery skips `.git`, `tests`, `test`, `fixtures`, `node_modules`, and Bethesda masters. |
| `esl` | `any` | `any`, `require`, or `forbid` |
| `tag` | pushed tag | Release tag. Defaults to `github.ref_name` on tag events, otherwise a tag at HEAD. |
| `tag-prefix` | `v` | Stripped from the tag before comparing |
| `require-tag` | `false` | Fail when no tag can be found |
| `changelog` | discover | `CHANGELOG.md`, `CHANGELOG`, `CHANGELOG.txt`, `CHANGES.md`, or `docs/CHANGELOG.md` |
| `fomod` | discover | Directory containing `ModuleConfig.xml` |
| `scan` | `path` | Directories or zips to scan for forbidden files |
| `allow` | | Allowlist globs (case-insensitive; matched against the relative path or the basename; `*` also matches `/`) |
| `forbid` | | Extra forbidden globs |
| `against` | | Other mod directories for the conflict report |
| `conflicts-fail-on` | `collision` | mod-conflict-graph severity that becomes an error: `collision`, `probable`, `warning`, or `never` |
| `require-conflicts` | `false` | Fail if mod-conflict-graph is unavailable |
| `mod-conflict-graph-path` | | Local checkout to use instead of downloading |
| `mod-conflict-graph-source` | upstream URL | Git URL to fetch from; set it to `''` to never download |
| `skip` | | Checks to skip |
| `advisory` | | Checks whose errors become warnings |
| `fail-on` | `error` | `error`, `warning`, or `never` |
| `python` | `python3` | Interpreter |

### Outputs

| Output | Description |
| --- | --- |
| `result` | `pass` or `fail` |
| `errors`, `warnings` | Finding counts |
| `report` | Path to the Markdown report |
| `json` | Path to the JSON result (findings, plugin facts, versions) |

The Markdown report is appended to `$GITHUB_STEP_SUMMARY`. Errors and warnings also appear as workflow annotations. The step exits nonzero when the gate fails: 1 for failed checks, 2 for usage or input errors.

### How mod-conflict-graph is loaded

The first step looks for mod-conflict-graph in this order:

1. The `mod-conflict-graph-path` checkout.
2. An already importable `modconflict` package.
3. `pip install --target $RUNNER_TEMP/...` from `mod-conflict-graph-source`.
4. A shallow `git clone` of the same URL.

pip and git each run under `nice` with a time limit. If all of them fail, the step still succeeds and the conflict check reports `CONFLICTS-UNAVAILABLE` as a notice, unless `require-conflicts: true` is set. mod-conflict-graph is built for Skyrim SE, so Fallout 4 results are marked best-effort.

## Running locally

```sh
python3 scripts/mod_release_gate.py --game skyrimse --root path/to/MyMod \
  --tag v1.2.0 --esl require --allow 'Source/Scripts/*.psc' \
  --mod-conflict-graph ../mod-conflict-graph --against ../OtherMod
python3 scripts/mod_release_gate.py --help
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests build minimal TES4 plugin binaries in memory (see [`tests/fixtures.py`](tests/fixtures.py)), along with FOMOD trees, changelogs, git repositories, and zips, all in temporary directories. No game files are used or shipped. `tests/test_action.py` extracts the composite steps' bash from `action.yml` and runs it. One integration test runs against a real mod-conflict-graph checkout when `MOD_CONFLICT_GRAPH_PATH` is set; otherwise it is skipped.

## Limits

- This is structural validation only. It does not inspect BSA/BA2 contents, compare file hashes against game data, or evaluate FOMOD conditions.
- Bethesda files are recognised by name. A renamed copy of a game master is not detected.
- The record walk reads record headers only. Compressed records are not decompressed, because ESL checks need only form IDs.
- The ESL range for Fallout 4 HEDR 1.0 follows the current Creation Kit behaviour. If your target runtime is older, keep HEDR at 0.95.

## License

[MIT](LICENSE) © 2026 synthalorian
