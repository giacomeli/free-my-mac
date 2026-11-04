"""RemoveMacAI tweaks: free-my-mac lists what is missing and delegates (apply, undo, background).

RemoveMacAI already maintains 41 tweaks with undo and its own configuration profile. Reimplementing them would
create two profiles fighting over the same keys, so this module only detects them and calls the tool.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..core import AVAILABLE, CLEAN, ERROR, PENDING, Context, Detection, Item, Op, Step
from ..localconfig import own_agent_prefixes

INSTALL_HINT = ('RemoveMacAI is not installed; install it with the apple-intelligence step of the cleanup '
                '(sh free-my-mac.sh).')
PROFILE_NOTE = 'via profile: macOS asks you to approve the updated profile in System Settings'

# Item labels are the RemoveMacAI titles; these notes add what a title does not say.
NOTES: Dict[str, str] = {
    'key-repeat': 'if you type accents by holding a key, you lose that menu',
    'hidden-files': 'shows .DS_Store, .git and similar files in every folder',
    'trash-30-days': 'permanently deletes anything that has been in the Trash for more than 30 days',
    'media-keys': 'the keys stop controlling any player, not just Music',
    'save-locally': 'new documents go to this Mac instead of iCloud',
}


def rmai_tool(ctx: Context) -> Optional[str]:
    return ctx.tool('removemacai')


def rmai_tweaks(ctx: Context) -> Tuple[Optional[List[dict]], str]:
    """RemoveMacAI tweak catalog with the state of each tweak; (None, reason) if it cannot be read."""
    tool = rmai_tool(ctx)
    if not tool:
        return None, INSTALL_HINT
    result = ctx.query([tool, 'tweaks', '--json'])
    if not result.ok:
        return None, 'removemacai tweaks --json failed: ' + (result.err or result.out).strip()[:200]
    try:
        data = json.loads(result.out)
    except ValueError as exc:
        return None, 'unexpected output from removemacai tweaks --json (%s)' % exc
    if not isinstance(data, list):
        return None, 'unexpected output from removemacai tweaks --json'
    return [t for t in data if isinstance(t, dict) and t.get('id')], ''


class RemoveMacAiTweaks(Step):
    rmai_group = ''
    group = 'rmai'

    def detect(self, ctx: Context) -> Detection:
        tweaks, problem = rmai_tweaks(ctx)
        if tweaks is None:
            return Detection(ERROR, summary=problem)
        mine = [t for t in tweaks if t.get('group') == self.rmai_group]
        items: List[Item] = []
        waiting: List[str] = []
        for tweak in mine:
            state = tweak.get('state')
            if state == 'applied':
                continue
            if state != 'notApplied':
                waiting.append('%s (%s)' % (tweak['id'], state))
                continue
            items.append(Item('tweak:' + tweak['id'], tweak.get('title') or tweak['id'],
                              note=NOTES.get(tweak['id'], ''), selected='recommended' in (tweak.get('presets') or []),
                              data={'id': tweak['id'], 'profile': bool(tweak.get('profile'))}))
        applied = sum(1 for t in mine if t.get('state') == 'applied')
        notes = ['Already applied: %d of %d.' % (applied, len(mine))] if applied else []
        profiled = [i for i in items if i.data['profile']]
        if profiled and len(profiled) == len(items):
            notes.append('All of them go through the RemoveMacAI profile: macOS asks you to approve the updated '
                         'profile in System Settings > General > Device Management.')
        else:
            for item in profiled:
                item.note = '; '.join(n for n in (item.note, PROFILE_NOTE) if n)
        if waiting:
            notes.append('In another state in RemoveMacAI: ' + ', '.join(waiting))
        if not items:
            return Detection(CLEAN, summary='All tweaks in this group are already applied.', notes=notes)
        return Detection(PENDING if any(i.selected for i in items) else AVAILABLE, items, notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        tool = rmai_tool(ctx) or 'removemacai'
        ids = [i.data['id'] for i in items]
        return [Op('Apply with RemoveMacAI', 'removemacai apply %s --yes' % ' '.join(ids),
                   argv=[tool, 'apply'] + ids + ['--yes'])]


def _tweak_step(rmai_group: str, step_id: str, title: str, why: str) -> type:
    return type('RemoveMacAi' + rmai_group.title(), (RemoveMacAiTweaks,), {
        'id': step_id,
        'rmai_group': rmai_group,
        'title': title,
        'what': ('Applies the checked tweaks in this group (%s) through RemoveMacAI. The tweaks in its '
                 'recommended preset come checked; the others are your choice.' % title),
        'why': why,
        'consequences': 'Each item says what changes. Privacy tweaks go through the RemoveMacAI profile.',
        'undo': 'removemacai undo <ids> (or removemacai revert to undo everything it changed).',
        'rerunnable': True,
        'clean_label': 'Already applied',
    })


RmaiPrivacy = _tweak_step('privacy', 'rmai-privacy', 'Privacy: analytics, ads and suggestions',
                          'Stops sending usage data and the suggestions that query Apple servers.')
RmaiAnnoyances = _tweak_step('annoyances', 'rmai-annoyances', 'System annoyances',
                             'Removes behaviors that get in the way more than they help.')
RmaiApps = _tweak_step('apps', 'rmai-apps', 'Apple apps', 'Hides Apple services you do not use.')
RmaiFinder = _tweak_step('finder', 'rmai-finder', 'Finder', 'Makes Finder more informative for developers.')
RmaiWindows = _tweak_step('windows', 'rmai-windows', 'Dock and windows', 'Less waiting and fewer distractions.')
RmaiTyping = _tweak_step('typing', 'rmai-typing', 'Typing', 'Keeps the system from changing what you type.')


BACKGROUND_LINE = re.compile(r'^\s+(?P<label>[\w.@-]+)\s{2,}(?P<name>\S.*?)\s{2,}(?P<state>on|off)\s+'
                             r'(?P<kind>\w+)(?P<all>\s+\(all users\))?\s*$')
# Left out of the list: the security module required by Brazilian banks (Warsaw), XQuartz, which the
# cleanup pkg-leftovers step removes entirely, and the user's own agents (own_agent_prefixes in local.json).
BACKGROUND_SKIP = ('com.topazofd.warsaw', 'org.xquartz.')
BACKGROUND_NOTES: Tuple[Tuple[str, str], ...] = (
    ('com.microsoft.', 'Office updater: Word, Excel and Teams stop updating themselves'),
    ('com.google.', 'Chrome updater: Chrome stops updating itself'),
    ('com.brother.', 'Brother printers and scanners may stop working'),
    ('com.crystalidea.macsfancontrol', 'Macs Fan Control stops controlling the fans'),
    ('io.macfuse.', 'macFUSE file systems (sshfs, NTFS) stop mounting'),
    ('net.tunnelblick.', 'the Tunnelblick VPN stops connecting'),
    ('homebrew.mxcl.ollama', 'ollama no longer starts at login; you can run ollama serve by hand'),
    ('com.koekeishiya.skhd', 'skhd keyboard shortcuts stop working'),
)


def background_items(text: str) -> List[dict]:
    """Lines of `removemacai background`: label, name, state, kind and whether it applies to all users."""
    rows = []
    for line in text.splitlines():
        match = BACKGROUND_LINE.match(line)
        if match:
            rows.append({'label': match.group('label'), 'name': match.group('name').strip(),
                         'state': match.group('state'), 'kind': match.group('kind'),
                         'all_users': bool(match.group('all'))})
    return rows


class BackgroundItems(Step):
    id = 'background-items'
    group = 'background'
    title = 'Background items from other apps'
    what = ('Turns off the checked background items (agents, helpers and updaters from other apps) through '
            'RemoveMacAI. Nothing comes checked: each item says what stops working.')
    why = 'Each item runs from login, uses memory and can wake the Mac.'
    consequences = ('The app that owns an item may stop working or updating. Left out of the list: your own '
                    'agents (own_agent_prefixes in ~/.config/free-my-mac/local.json), the security module '
                    'required by banks (Warsaw) and XQuartz, which the cleanup pkg-leftovers step removes.')
    undo = 'removemacai background on <label>.'
    rerunnable = True
    clean_label = 'Already applied'

    def detect(self, ctx: Context) -> Detection:
        tool = rmai_tool(ctx)
        if not tool:
            return Detection(ERROR, summary=INSTALL_HINT)
        result = ctx.query([tool, 'background'])
        if not result.ok:
            return Detection(ERROR, summary='removemacai background failed: ' + (result.err or result.out).strip()[:200])
        rows = background_items(result.out)
        skip = BACKGROUND_SKIP + own_agent_prefixes(ctx)
        items: List[Item] = []
        for row in rows:
            if row['state'] != 'on' or row['label'].startswith(skip):
                continue
            note = next((n for prefix, n in BACKGROUND_NOTES if row['label'].startswith(prefix)),
                        '%s may stop working' % row['name'])
            if row['label'].startswith('com.microsoft.') and not _has_microsoft_apps(ctx):
                note = 'no Microsoft apps installed: leftover from an old install'
            items.append(Item('bg:' + row['label'], '%s (%s, %s%s)' % (
                row['label'], row['name'], row['kind'], ', all users' if row['all_users'] else ''),
                note=note, selected=False, data={'label': row['label']}))
        if not items:
            return Detection(CLEAN, summary='No third-party background items are on.')
        return Detection(AVAILABLE, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        tool = rmai_tool(ctx) or 'removemacai'
        return [Op('Turn off ' + i.data['label'], 'removemacai background off ' + i.data['label'],
                   argv=[tool, 'background', 'off', i.data['label']]) for i in items]


def _has_microsoft_apps(ctx: Context) -> bool:
    folder: Path = ctx.sys('/Applications')
    return folder.is_dir() and any(p.name.startswith('Microsoft ') for p in folder.glob('*.app'))


STEPS = [RmaiPrivacy, RmaiAnnoyances, RmaiApps, RmaiFinder, RmaiWindows, RmaiTyping, BackgroundItems]
