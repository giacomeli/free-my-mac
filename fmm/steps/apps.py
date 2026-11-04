"""Unused Nerd Font families and Chrome's local AI model."""
from __future__ import annotations

import os
import plistlib
import re
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..core import CLEAN, PENDING, Context, Detection, Item, Op, Step, digest, plural
from ..fs import safe_remove, write_atomic
from ..launchd import read_plist
from ..paths import remove_paths_op

FONTS = '~/Library/Fonts'
NERD = re.compile(r'^(?P<family>.+?)\s*Nerd\s*Font', re.IGNORECASE)
# Where editors and terminals store the chosen font: (file, regex with the name in group 1).
FONT_SETTINGS = (
    ('~/Library/Application Support/Code/User/settings.json', r'"(?:editor|terminal\.integrated)\.fontFamily"\s*:\s*"([^"]*)"'),
    ('~/Library/Application Support/Cursor/User/settings.json', r'"(?:editor|terminal\.integrated)\.fontFamily"\s*:\s*"([^"]*)"'),
    ('~/.config/zed/settings.json', r'"(?:buffer|ui|terminal)_font_family"\s*:\s*"([^"]*)"'),
    ('~/.config/ghostty/config', r'^\s*font-family\s*=\s*"?([^"\n]+)"?'),
    ('~/Library/Application Support/com.mitchellh.ghostty/config', r'^\s*font-family\s*=\s*"?([^"\n]+)"?'),
    ('~/.config/alacritty/alacritty.toml', r'family\s*=\s*"([^"]+)"'),
    ('~/.config/kitty/kitty.conf', r'^\s*font_family\s+(.+)$'),
    ('~/.wezterm.lua', r'font(?:_with_fallback)?\s*\(\s*\{?\s*[\'"]([^\'"]+)[\'"]'),
)
ITERM_FONT = re.compile(r'"(?:Normal|Non Ascii) Font"\s*=\s*"([^"]+)"')


def font_base(name: str) -> str:
    """Comparable family name: 'MesloLGS NF', 'MesloLGS-NF-Regular 12' and 'MesloLGSNerdFont' -> 'meslolgs'."""
    name = re.split(r'NerdFont|Nerd Font|[- ]NF\b|NF-', name.strip().strip('\'"'))[0]
    name = re.sub(r'\s+\d+(\.\d+)?$', '', name)  # size at the end (iTerm2)
    name = name.split('-')[0]
    return re.sub(r'[^a-z0-9]', '', name.lower())


def used_fonts(ctx: Context) -> Set[str]:
    """Families named in editor and terminal settings."""
    names: List[str] = []
    for spec, pattern in FONT_SETTINGS:
        path = ctx.user(spec)
        if not path.is_file():
            continue
        text = path.read_text(errors='replace')
        for match in re.finditer(pattern, text, re.MULTILINE):
            names.extend(n for n in match.group(1).split(','))
    iterm = ctx.query(['/usr/bin/defaults', 'read', 'com.googlecode.iterm2', 'New Bookmarks'])
    if iterm.ok:
        names.extend(m.group(1) for m in ITERM_FONT.finditer(iterm.out))
    return {b for b in (font_base(n) for n in names) if len(b) >= 4}


def cask_fonts(ctx: Context) -> Set[str]:
    """Families installed by cask (font-<name>-nerd-font): Homebrew manages them."""
    bases = set()
    for cask in ctx.brew_casks():
        match = re.match(r'^font-(.+?)(?:-nerd-font)?$', cask)
        if match:
            bases.add(re.sub(r'[^a-z0-9]', '', match.group(1)))
    return bases


class NerdFonts(Step):
    id = 'nerd-fonts'
    group = 'apps'
    title = 'Unused Nerd Fonts'
    what = ('Deletes from ~/Library/Fonts the Nerd Font families that no editor or terminal uses. It keeps the '
            'families named in the VS Code, Cursor, Zed, iTerm2, Ghostty, Alacritty, kitty and WezTerm settings, '
            'the ones installed by Homebrew cask, and every font that is not a Nerd Font.')
    why = 'Each Nerd Font family has dozens of variants; together they take up several GB.'
    consequences = ('Apps that use one of these fonts fall back to the default font. The macOS Terminal is not '
                    'checked: if it uses a Nerd Font, uncheck that family.')
    undo = 'brew install --cask font-<name>-nerd-font, or download it from nerdfonts.com.'

    def detect(self, ctx: Context) -> Detection:
        folder = ctx.user(FONTS)
        if not folder.is_dir():
            return Detection(CLEAN, summary='No user fonts.')
        keep = used_fonts(ctx) | cask_fonts(ctx)
        families: Dict[str, List[Path]] = {}
        names: Dict[str, str] = {}
        for entry in sorted(folder.iterdir()):
            match = NERD.match(entry.stem)
            if not match or not entry.is_file():
                continue
            base = font_base(match.group('family'))
            families.setdefault(base, []).append(entry)
            names.setdefault(base, match.group('family').strip())
        items: List[Item] = []
        kept: List[str] = []
        for base, files in sorted(families.items()):
            if any(base.startswith(k) for k in keep):
                kept.append(names[base])
                continue
            size = sum(f.lstat().st_blocks * 512 for f in files)
            items.append(Item('family:%s:%s' % (base, digest(f.name for f in files)),
                              '%s (%s)' % (names[base], plural(len(files), 'file', 'files')), size=size,
                              data={'files': [str(f) for f in files]}))
        notes = ['Kept (in use or from Homebrew): ' + ', '.join(kept)] if kept else []
        return Detection(PENDING if items else CLEAN, items, notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        files = [Path(f) for i in items for f in i.data['files']]

        def remove(c: Context, targets=tuple(files)) -> None:
            for target in targets:
                safe_remove(target)
            print('  %s removed' % plural(len(targets), 'font file', 'font files'), flush=True)
        return [Op('Delete %s' % plural(len(items), 'Nerd Font family', 'Nerd Font families'),
                   'rm ~/Library/Fonts/<family>*Nerd Font* (%s)' % plural(len(files), 'file', 'files'),
                   func=remove)]


CHROME_APP = '/Applications/Google Chrome.app'
CHROME_MODEL = '~/Library/Application Support/Google/Chrome/OptGuideOnDeviceModel'
# ManagedClient generates the files in this folder from the installed configuration profiles. Writing
# here with `defaults write` does not work: cfprefsd returns 0 and discards the value.
MANAGED = '/Library/Managed Preferences/com.google.Chrome'
POLICY = 'GenAILocalFoundationalModelSettings'
# PROFILE_ID and the UUIDs derived from it identify the installed profile; changing them would install a
# second profile instead of replacing the existing one.
PROFILE_ID = 'local.free-my-mac.chrome-ai'
PROFILE_NAME = 'Chrome without local AI (free-my-mac)'
PROFILE_FILE = 'ChromeSemIALocal.mobileconfig'
PROFILES_PANE = 'x-apple.systempreferences:com.apple.Profiles-Settings.extension'
WAIT_LABEL = 'Wait for profile approval'
APPROVAL_TIMEOUT = 600  # seconds
APPROVAL_POLL = 2


def chrome_policy(ctx: Context) -> Optional[int]:
    data = read_plist(ctx.sys(MANAGED + '.plist'))
    value = (data or {}).get(POLICY)
    return value if isinstance(value, int) else None


def chrome_profile() -> bytes:
    """System configuration profile that forces the policy on Chrome (like the RemoveMacAI one)."""
    preferences = PROFILE_ID + '.preferences'
    return plistlib.dumps({
        'PayloadType': 'Configuration',
        'PayloadVersion': 1,
        'PayloadIdentifier': PROFILE_ID,
        'PayloadUUID': str(uuid.uuid5(uuid.NAMESPACE_DNS, PROFILE_ID)).upper(),
        'PayloadDisplayName': PROFILE_NAME,
        'PayloadDescription': 'Prevents Google Chrome from downloading the local AI model (%s = 1). '
                              'Remove this profile to undo.' % POLICY,
        'PayloadOrganization': 'free-my-mac',
        'PayloadScope': 'System',
        'PayloadRemovalDisallowed': False,
        'PayloadContent': [{
            'PayloadType': 'com.apple.ManagedClient.preferences',
            'PayloadVersion': 1,
            'PayloadIdentifier': preferences,
            'PayloadUUID': str(uuid.uuid5(uuid.NAMESPACE_DNS, preferences)).upper(),
            'PayloadDisplayName': 'Google Chrome policy',
            'PayloadContent': {'com.google.Chrome': {'Forced': [{'mcx_preference_settings': {POLICY: 1}}]}},
        }],
    })


class ChromeAiModel(Step):
    id = 'chrome-ai-model'
    group = 'apps'
    title = 'Chrome local AI model'
    what = ('Installs a configuration profile that forces the %s = 1 policy ("do not download the model") on '
            'Chrome, and deletes the Gemini Nano model already downloaded (OptGuideOnDeviceModel). macOS only '
            'accepts a forced policy through a profile, and only you can approve the profile: the step opens '
            'System Settings and waits up to %d minutes. The model is deleted only after approval.'
            % (POLICY, APPROVAL_TIMEOUT // 60))
    why = 'The model takes about 4 GB and, without the policy, Chrome downloads it again.'
    consequences = ('Chrome loses its on-device AI features (such as "Help me write") and shows "Managed by '
                    'your organization". The step only runs with Chrome closed. Check chrome://policy (Reload '
                    'policies): %s should show with value 1.' % POLICY)
    undo = ('System Settings > General > Device Management > "%s" > Remove '
            '(or sudo profiles remove -identifier %s).' % (PROFILE_NAME, PROFILE_ID))

    def detect(self, ctx: Context) -> Detection:
        model = ctx.user(CHROME_MODEL)
        installed = ctx.sys(CHROME_APP).exists()
        items: List[Item] = []
        if installed and chrome_policy(ctx) != 1:
            items.append(Item('policy', 'Profile "%s" with the %s = 1 policy' % (PROFILE_NAME, POLICY)))
        if os.path.lexists(model):
            items.append(Item('path:' + str(model), ctx.pretty(model), path=str(model), size=ctx.du(model)))
        if not items:
            return Detection(CLEAN, summary='No downloaded model, and the policy is in place.' if installed
                             else 'Chrome is not installed.')
        return Detection(PENDING, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        def ensure_closed(c: Context) -> None:
            running = c.query(['/usr/bin/pgrep', '-x', 'Google Chrome'], cache=False)
            if running.ok and running.out.strip():
                raise RuntimeError('Chrome is open; quit it with Cmd+Q and run again')
        ops = [Op('Check that Chrome is closed', 'pgrep -x "Google Chrome"', func=ensure_closed)]
        if any(i.key == 'policy' for i in items):
            profile = ctx.backup_dir() / 'chrome' / PROFILE_FILE

            def write_profile(c: Context) -> None:
                profile.parent.mkdir(parents=True, exist_ok=True)
                for folder in (c.backup_dir(), profile.parent):
                    os.chmod(folder, 0o700)
                write_atomic(profile, chrome_profile())
                print('  profile written to ' + c.pretty(profile), flush=True)

            def wait_approval(c: Context) -> None:
                print('  In System Settings > General > Device Management, double-click\n'
                      '  "%s", then click Install and confirm with your password.\n'
                      '  Waiting up to %d minutes (Ctrl-C stops)...' % (PROFILE_NAME, APPROVAL_TIMEOUT // 60),
                      flush=True)
                deadline = time.monotonic() + APPROVAL_TIMEOUT
                while chrome_policy(c) != 1:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('the profile was not approved in time; the model was kept. Approve it '
                                           'in Device Management and run the step again')
                    time.sleep(APPROVAL_POLL)
                print('  profile approved: %s = 1 is in effect' % POLICY, flush=True)

            ops.append(Op('Generate the configuration profile', 'write ' + ctx.pretty(profile), func=write_profile))
            ops.append(Op('Open the profile', 'open ' + ctx.pretty(profile), argv=['/usr/bin/open', str(profile)]))
            ops.append(Op('Open Device Management', 'open "%s"' % PROFILES_PANE,
                          argv=['/usr/bin/open', PROFILES_PANE], tolerate_failure=True))
            ops.append(Op(WAIT_LABEL, 'wait for %s = 1 in "%s.plist"' % (POLICY, MANAGED), func=wait_approval))
        model = [Path(i.path) for i in items if i.key.startswith('path:')]
        ops.extend(remove_paths_op(ctx, model, 'Delete the downloaded model'))
        return ops


STEPS = [NerdFonts, ChromeAiModel]
