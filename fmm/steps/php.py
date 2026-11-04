"""PHP and Herd: full removal (nothing is reinstalled)."""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import List

from ..core import CLEAN, PENDING, Context, Detection, Item, Op, Step, plural
from ..fs import write_atomic, write_private
from ..paths import PathsStep


class Herd(PathsStep):
    id = 'herd'
    group = 'php'
    title = 'Remove Herd'
    quit_apps = (('Herd', 'de.beyondco.herd'),)
    targets = ('/Applications/Herd.app',
               '~/Library/Application Support/Herd', '~/Library/Application Support/de.beyondco.herd',
               '~/Library/Preferences/de.beyondco.herd.plist', '~/Library/Caches/de.beyondco.herd',
               '~/Library/HTTPStorages/de.beyondco.herd', '~/Library/Saved Application State/de.beyondco.herd.savedState',
               '~/Library/Logs/Herd', '~/Library/LaunchAgents/de.beyondco.herd*',
               '/Library/LaunchDaemons/de.beyondco.herd*', '/Library/PrivilegedHelperTools/de.beyondco.herd*',
               '~/.config/herd', '~/.config/herd-lite', '~/.config/valet', '~/.valet', '~/Herd')
    what = 'Quits and deletes Herd (app, data, preferences, caches, logs, services) and the Valet configs.'
    why = 'You decided to remove PHP completely. Herd was still installed after the manual uninstall.'
    consequences = ('Local .test sites served by Herd stop responding. The ~/Herd folder is only removed if it '
                    'is empty; no project code is touched.')
    undo = 'Download Herd again from herd.laravel.com.'

    def target_paths(self, ctx: Context) -> List[Path]:
        sites = ctx.user('~/Herd')
        paths = super().target_paths(ctx)
        if sites in paths and [e for e in os.listdir(sites) if e != '.DS_Store']:
            paths.remove(sites)
        return paths


class PhpLeftovers(PathsStep):
    id = 'php-leftovers'
    group = 'php'
    title = 'Remove PHP and Composer leftovers'
    formulas = ('php', 'php@8.1', 'php@8.2', 'php@8.3', 'php@8.4', 'composer')
    taps = ('shivammathur/php',)
    targets = ('/usr/local/bin/php', '/usr/local/bin/php-fpm', '/usr/local/etc/php', '/opt/homebrew/etc/php',
               '~/.composer', '~/.config/composer', '~/.cache/composer', '~/Library/Caches/composer',
               '~/.pearrc', '~/.laradumps', '~/.config/psysh', '~/.config/tinkerwell', '~/.cache/phpactor')
    what = ('Deletes the /usr/local/bin/php wrapper (which called a Docker container that no longer exists), '
            'orphaned php.ini configs, Composer globals and cache, psysh, tinkerwell, laradumps and the '
            'phpactor cache. Uninstalls PHP and Composer from Homebrew, if present.')
    why = 'There is no working PHP on this machine today; these files only confuse the PATH and take up space.'
    consequences = ('The php command goes away. The composer from the old Intel Homebrew is removed in the '
                    '"Intel Homebrew" step.')
    undo = 'brew install php composer, or Herd, when you need it again.'


def test_only(line: str) -> bool:
    """Hosts line whose names are all .test (never a comment or an empty line)."""
    content = line.split('#', 1)[0].split()
    if len(content) < 2:
        return False
    return all(name.lower().endswith('.test') for name in content[1:])


class HostsTest(Step):
    id = 'hosts-test'
    group = 'php'
    title = 'Remove .test domains from /etc/hosts'
    requires = ('backup',)
    what = 'Removes from /etc/hosts the lines that only contain .test domains (the local Herd and Valet sites).'
    why = 'Without Herd, these names point to 127.0.0.1 with no server behind them.'
    consequences = ('Lines with other names, comments and localhost are kept. The DNS cache is flushed at '
                    'the end.')
    undo = ('A copy of the original is kept in ~/bkp/free-my-mac/<date>/etc-before/: '
            'sudo cp <copy> /etc/hosts.')

    def detect(self, ctx: Context) -> Detection:
        hosts = ctx.sys('/etc/hosts')
        if not hosts.is_file():
            return Detection(CLEAN, summary='/etc/hosts not found.')
        items = []
        for line in hosts.read_text(errors='replace').splitlines():
            if test_only(line):
                digest = hashlib.sha1(line.strip().encode()).hexdigest()[:12]
                items.append(Item('line:' + digest, line.strip(), data={'line': line.strip()}))
        return Detection(PENDING if items else CLEAN, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        remove = {i.data['line'] for i in items}

        def rewrite(c: Context) -> None:
            hosts = c.sys('/etc/hosts')
            original = hosts.read_text(errors='replace')
            kept = [line for line in original.splitlines() if line.strip() not in remove]
            text = '\n'.join(kept) + '\n'
            snapshot = c.backup_dir() / 'etc-before' / ('hosts.%s' % time.strftime('%H%M%S'))
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(c.backup_dir(), 0o700)
            write_private(snapshot, original)
            print('  copy of the original: ' + c.pretty(snapshot), flush=True)
            if not c.real_system:
                write_atomic(hosts, text)
                return
            c.state_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile('w', dir=str(c.state_dir), prefix='hosts-', delete=False) as tmp:
                tmp.write(text)
            staged = str(hosts) + '.free-my-mac-new'
            try:
                # Write next to it and swap with mv: an interruption never leaves /etc/hosts half-written.
                if c.call(['/usr/bin/sudo', '/usr/bin/install', '-m', '644', '-o', 'root', '-g', 'wheel',
                           tmp.name, staged]):
                    raise RuntimeError('could not prepare the new /etc/hosts')
                if c.call(['/usr/bin/sudo', '/bin/mv', '-f', staged, str(hosts)]):
                    c.call(['/usr/bin/sudo', '/bin/rm', '-f', staged])
                    raise RuntimeError('could not replace /etc/hosts')
            finally:
                Path(tmp.name).unlink()
            c.call(['/usr/bin/sudo', '/usr/bin/dscacheutil', '-flushcache'])
            c.call(['/usr/bin/sudo', '/usr/bin/killall', '-HUP', 'mDNSResponder'])

        return [Op('Remove %s from /etc/hosts' % plural(len(remove), '.test line', '.test lines'),
                   'sudo install -m 644 <hosts without the .test lines> /etc/hosts.free-my-mac-new && '
                   'sudo mv -f /etc/hosts.free-my-mac-new /etc/hosts && sudo dscacheutil -flushcache',
                   func=rewrite, sudo=True)]


STEPS = [Herd, PhpLeftovers, HostsTest]
