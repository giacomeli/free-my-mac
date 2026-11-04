"""iOS simulators: every runtime, every device and the dyld cache."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

from ..core import CLEAN, ERROR, PENDING, Context, Detection, Item, Op, Step, digest, human, plural
from ..paths import remove_paths_op

ORPHAN_ASSETS = '/System/Library/AssetsV2/com_apple_MobileAsset_iOSSimulatorRuntime'


class Simulators(Step):
    id = 'simulators'
    group = 'sim'
    title = 'Remove iOS simulators'
    what = ('Deletes every simulator runtime (iOS 17.0, 17.5, 18.4, 26.5), every simulated device and the '
            'simulator dyld cache.')
    why = 'You decided to remove all of them. Together they take close to 40 GB.'
    consequences = ('Xcode stays installed, but it cannot run apps in the simulator until it downloads a '
                    'runtime again. Data on the simulated devices is lost.')
    undo = 'Xcode > Settings > Components downloads the runtime again (about 8 GB each).'

    def detect(self, ctx: Context) -> Detection:
        xcrun = ctx.tool('xcrun')
        if not xcrun:
            return Detection(CLEAN, summary='xcrun not found (no Xcode or Command Line Tools).')
        items: List[Item] = []
        runtimes = ctx.query([xcrun, 'simctl', 'runtime', 'list', '-j'], timeout=60)
        if not runtimes.ok:
            return Detection(ERROR, summary='simctl runtime list failed: ' + runtimes.err.strip()[:200])
        data = json.loads(runtimes.out or '{}')
        records = data.values() if isinstance(data, dict) else data
        for record in sorted(records, key=lambda r: r.get('version', '')):
            identifier = record.get('identifier', '')
            if not identifier or not record.get('deletable', True):
                continue
            platform = 'iOS' if 'iphone' in record.get('platformIdentifier', '') else record.get('platformIdentifier', '').split('.')[-1]
            items.append(Item('runtime:' + identifier, 'Runtime %s %s (%s)' % (platform, record.get('version', '?'),
                                                                               record.get('build', '?')),
                              size=record.get('sizeBytes'), note='last used: ' + (record.get('lastUsedAt') or 'never')[:10]))
        devices = ctx.query([xcrun, 'simctl', 'list', 'devices', '-j'], timeout=60)
        if devices.ok:
            udids = [d.get('udid', '') for v in json.loads(devices.out or '{}').get('devices', {}).values() for d in v]
            count = len(udids)
            if count:
                items.append(Item('devices:' + digest(udids),
                                  plural(count, 'simulated device and its data', 'simulated devices and their data'),
                                  size=ctx.du(ctx.user('~/Library/Developer/CoreSimulator/Devices'))))
        dyld = ctx.sys('/Library/Developer/CoreSimulator/Caches/dyld')
        if dyld.exists():
            items.append(Item('dyld', 'Simulator dyld cache (' + ctx.pretty(dyld) + ')', path=str(dyld),
                              size=ctx.du(dyld)))
        notes: List[str] = []
        if not any(i.key.startswith('runtime:') for i in items):
            orphan = ctx.sys(ORPHAN_ASSETS)
            assets = sorted(orphan.glob('*.asset')) if orphan.is_dir() else []
            if assets:
                size = sum(ctx.du(a) or 0 for a in assets)
                notes.append('An orphan runtime was left in %s (%s). It does not show up in simctl and the folder '
                             'is protected by SIP: there is no supported way to remove it (a known Apple bug). It '
                             'stays out of this step.' % (ctx.pretty(orphan), human(size)))
        return Detection(PENDING if items else CLEAN, items, notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        xcrun = ctx.tool('xcrun') or 'xcrun'
        ops: List[Op] = []
        if any(i.key.startswith('devices:') for i in items):
            ops.append(Op('Delete all simulated devices', 'xcrun simctl delete all',
                          argv=[xcrun, 'simctl', 'delete', 'all']))
        for item in items:
            if item.key.startswith('runtime:'):
                identifier = item.key.split(':', 1)[1]
                ops.append(Op('Delete ' + item.label, 'xcrun simctl runtime delete ' + identifier,
                              argv=[xcrun, 'simctl', 'runtime', 'delete', identifier]))
        dyld = [i for i in items if i.key == 'dyld']
        if dyld:
            ops.extend(remove_paths_op(ctx, [Path(dyld[0].path)], 'Delete the simulator dyld cache'))
        return ops


STEPS = [Simulators]
