"""Tweaks RemoveMacAI does not cover: animations, firewall stealth mode and Power Nap on battery."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Union

from ..core import AVAILABLE, CLEAN, ERROR, PENDING, Context, Detection, Item, Op, Step

DEFAULTS = '/usr/bin/defaults'
SOCKETFILTERFW = '/usr/libexec/ApplicationFirewall/socketfilterfw'
PMSET = '/usr/bin/pmset'


@dataclass(frozen=True)
class DefaultsTweak:
    key: str
    domain: str
    name: str
    kind: str  # float or bool
    value: Union[float, bool]
    label: str
    note: str = ''

    @property
    def restarts_dock(self) -> bool:
        return self.domain == 'com.apple.dock'

    def argv(self) -> List[str]:
        if self.kind == 'bool':
            return [DEFAULTS, 'write', self.domain, self.name, '-bool', 'true' if self.value else 'false']
        return [DEFAULTS, 'write', self.domain, self.name, '-float', '%g' % self.value]

    def current(self, ctx: Context) -> Optional[str]:
        result = ctx.query([DEFAULTS, 'read', self.domain, self.name], cache=False)
        return result.out.strip() if result.ok else None

    def applied(self, raw: Optional[str]) -> bool:
        if raw is None:
            return False
        if self.kind == 'bool':
            return raw.lower() in ('1', 'true', 'yes') if self.value else raw.lower() in ('0', 'false', 'no')
        try:
            return abs(float(raw) - float(self.value)) < 1e-6
        except ValueError:
            return False


UI_TWEAKS = (
    DefaultsTweak('dock-show', 'com.apple.dock', 'autohide-time-modifier', 'float', 0.15,
                  'Hidden Dock appears faster (0.15 s)', 'only matters when the Dock hides automatically'),
    DefaultsTweak('mission-control', 'com.apple.dock', 'expose-animation-duration', 'float', 0.15,
                  'Faster Mission Control (0.15 s)'),
    DefaultsTweak('fixed-spaces', 'com.apple.dock', 'mru-spaces', 'bool', False,
                  'Keep Spaces in a fixed order, not by recent use'),
    DefaultsTweak('window-resize', 'NSGlobalDomain', 'NSWindowResizeTime', 'float', 0.001,
                  'No animation for sheets and window resizing', 'applies to each app after it is reopened'),
    DefaultsTweak('quick-look', 'NSGlobalDomain', 'QLPanelAnimationDuration', 'float', 0,
                  'Quick Look opens without animation', 'applies to each app after it is reopened'),
)


class UiSpeed(Step):
    id = 'ui-speed'
    group = 'tuning'
    title = 'Shorter animations'
    clean_label = 'Already applied'
    what = ('Shortens or turns off the animations of the Dock, Mission Control, windows and Quick Look with '
            '`defaults write`, and restarts the Dock when one of its keys changes. Nothing comes checked: it is '
            'a matter of taste.')
    why = 'The default animations add waiting to every gesture; RemoveMacAI does not touch these keys.'
    consequences = 'Visual only. The Dock flickers when it restarts; open apps pick up the global keys when reopened.'
    undo = 'defaults delete <domain> <key> for each item (the value returns to the macOS default), then killall Dock.'
    rerunnable = True

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        for tweak in UI_TWEAKS:
            raw = tweak.current(ctx)
            if tweak.applied(raw):
                continue
            now = 'macOS default' if raw is None else raw
            note = 'current: %s; %s %s' % (now, tweak.domain, tweak.name)
            items.append(Item('ui:' + tweak.key, tweak.label, selected=False,
                              note='; '.join(n for n in (note, tweak.note) if n), data={'key': tweak.key}))
        if not items:
            return Detection(CLEAN, summary='All animations are already adjusted.')
        return Detection(AVAILABLE, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        chosen = [t for t in UI_TWEAKS if any(i.data['key'] == t.key for i in items)]
        ops = [Op(t.label, ' '.join(t.argv()).replace(DEFAULTS, 'defaults'), argv=t.argv()) for t in chosen]
        if any(t.restarts_dock for t in chosen):
            ops.append(Op('Restart the Dock', 'killall Dock', argv=['/usr/bin/killall', 'Dock'],
                          tolerate_failure=True))
        return ops


class FirewallStealth(Step):
    id = 'firewall-stealth'
    group = 'tuning'
    title = 'Firewall stealth mode'
    clean_label = 'Already applied'
    what = 'Turns on stealth mode in the macOS firewall (socketfilterfw --setstealthmode on).'
    why = ('On public networks, the Mac stops answering pings and probes to closed ports. The CIS Benchmark '
           'for macOS recommends it.')
    consequences = ('Pings to this Mac go unanswered, including on your home network. Services you allowed '
                    'in the firewall stay reachable.')
    undo = 'sudo %s --setstealthmode off' % SOCKETFILTERFW

    def detect(self, ctx: Context) -> Detection:
        stealth = ctx.query([SOCKETFILTERFW, '--getstealthmode'], cache=False)
        if not stealth.ok:
            return Detection(ERROR, summary='socketfilterfw failed: ' + (stealth.err or stealth.out).strip()[:200])
        if re.search(r'\b(is on|enabled)\b', stealth.out, re.IGNORECASE):
            return Detection(CLEAN, summary='Stealth mode is already on.')
        state = ctx.query([SOCKETFILTERFW, '--getglobalstate'], cache=False)
        notes = [] if 'enabled' in state.out.lower() else [
            'The firewall is off; stealth mode only works while it is on (System Settings > Network > Firewall).']
        return Detection(PENDING, [Item('stealth', 'Turn on stealth mode')], notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        argv = [SOCKETFILTERFW, '--setstealthmode', 'on']
        return [Op('Turn on stealth mode', 'sudo socketfilterfw --setstealthmode on', argv=argv, sudo=True)]


def battery_setting(text: str, name: str) -> Optional[str]:
    """Value of a key in the "Battery Power" section of `pmset -g custom`; None without a battery."""
    section = re.search(r'^Battery Power:\s*$(.*?)(?=^\S.*Power:\s*$|\Z)', text, re.MULTILINE | re.DOTALL)
    if not section:
        return None
    match = re.search(r'^\s*%s\s+(\S+)' % re.escape(name), section.group(1), re.MULTILINE)
    return match.group(1) if match else None


class BatteryPowerNap(Step):
    id = 'battery-powernap'
    group = 'tuning'
    title = 'Power Nap only on power adapter'
    clean_label = 'Already applied'
    what = 'Turns off Power Nap on battery (sudo pmset -b powernap 0). Nothing changes on the power adapter.'
    why = 'On battery, Power Nap wakes the Mac during sleep to sync, which drains the battery.'
    consequences = ('With the lid closed on battery, Mail, Calendar and iCloud only sync when the Mac wakes up. '
                    'It comes unchecked: your choice.')
    undo = 'sudo pmset -b powernap 1'

    def detect(self, ctx: Context) -> Detection:
        result = ctx.query([PMSET, '-g', 'custom'], cache=False)
        if not result.ok:
            return Detection(ERROR, summary='pmset failed: ' + (result.err or result.out).strip()[:200])
        value = battery_setting(result.out, 'powernap')
        if value is None:
            return Detection(CLEAN, summary='No battery or no Power Nap.')
        if value == '0':
            return Detection(CLEAN, summary='Power Nap is already off on battery.')
        return Detection(AVAILABLE, [Item('powernap', 'Turn off Power Nap on battery', selected=False)])

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        return [Op('Turn off Power Nap on battery', 'sudo pmset -b powernap 0',
                   argv=[PMSET, '-b', 'powernap', '0'], sudo=True)]


STEPS = [UiSpeed, FirewallStealth, BatteryPowerNap]
