"""Docker: full removal of Docker Desktop. Docker now runs on a remote server, over ssh."""
from __future__ import annotations

from pathlib import Path
from typing import List

from ..core import CLEAN, PENDING, Context, Detection, Item, Op, Step
from ..paths import PathsStep, links_into, quit_app_op, remove_paths_op


class DockerDesktop(Step):
    id = 'docker-desktop'
    group = 'docker'
    title = 'Uninstall Docker Desktop'
    what = ('Quits Docker Desktop, runs the official uninstaller (which deletes the VM and, with it, all '
            'images, containers, volumes and builders) and removes Docker.app.')
    why = 'You decided to use Docker only on a remote server, over ssh. Here it takes up a whole VM.'
    consequences = ('All local volumes are lost for good, including the ones with data (see the list under '
                    'Notes). The docker command no longer exists on the Mac.')
    undo = 'Reinstall Docker Desktop from the website; the volumes do not come back.'

    def app(self, ctx: Context) -> Path:
        return ctx.sys('/Applications/Docker.app')

    def detect(self, ctx: Context) -> Detection:
        app = self.app(ctx)
        if not app.exists():
            return Detection(CLEAN, summary='Docker.app is not in /Applications.')
        vm = ctx.user('~/Library/Containers/com.docker.docker')
        items = [Item('desktop', 'Docker Desktop and its VM (images, containers, volumes, builders)',
                      size=(ctx.du(vm) or 0) + (ctx.du(app) or 0), path=str(app))]
        notes: List[str] = []
        docker = ctx.tool('docker')
        if docker:
            volumes = ctx.query([docker, 'volume', 'ls', '--format', '{{.Name}}'], timeout=15)
            if volumes.ok:
                names = volumes.out.split()
                named = [n for n in names if not (len(n) == 64 and all(c in '0123456789abcdef' for c in n))]
                notes.append('Volumes to be deleted: %d named, %d anonymous.' % (len(named), len(names) - len(named)))
                if named:
                    notes.append('Named: ' + ', '.join(named))
            else:
                notes.append('Docker did not respond; could not list the volumes (the VM is deleted anyway).')
            images = ctx.query([docker, 'image', 'ls', '--format', '{{.Repository}}:{{.Tag}}'], timeout=15)
            if images.ok and images.out.strip():
                notes.append('Images: ' + ', '.join(images.out.split()))
        return Detection(PENDING, items, notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        app = self.app(ctx)
        uninstall = app / 'Contents/MacOS/uninstall'
        ops = [quit_app_op('Docker Desktop', 'com.docker.docker')]
        if uninstall.exists():
            ops.append(Op('Official Docker Desktop uninstaller', str(uninstall), argv=[str(uninstall)]))
        ops.extend(remove_paths_op(ctx, [app], 'Remove Docker.app'))
        return ops


class DockerLeftovers(PathsStep):
    id = 'docker-leftovers'
    group = 'docker'
    title = 'Remove Docker leftovers'
    requires = ('docker-desktop',)
    targets = ('~/.docker', '~/Library/Containers/com.docker.docker', '~/Library/Containers/com.docker.helper',
               '~/Library/Group Containers/group.com.docker',
               '~/Library/Application Support/Docker Desktop', '~/Library/Application Support/com.docker.docker',
               '~/Library/Caches/com.docker.docker', '~/Library/HTTPStorages/com.docker.docker',
               '~/Library/Preferences/com.docker.docker.plist', '~/Library/Preferences/com.electron.docker-frontend.plist',
               '~/Library/Saved Application State/com.electron.docker-frontend.savedState',
               '~/Library/Logs/Docker Desktop',
               '/Library/LaunchDaemons/com.docker.*', '/Library/PrivilegedHelperTools/com.docker.*',
               '/usr/local/lib/docker', '/etc/paths.d/docker')
    what = ('Removes what the uninstaller leaves behind: ~/.docker (contexts, settings, buildx), caches, '
            'preferences, logs, privileged services (vmnetd, socket) and the docker, kubectl, hub-tool etc. '
            'links in /usr/local/bin that pointed into Docker.app.')
    why = 'Without the app, these files and links are just broken leftovers.'
    consequences = ('kubectl goes away too (it came from Docker.app). Registry credentials saved in the Keychain '
                    'are not touched. The Docker completions block in .zshrc is removed in the Shell step.')
    undo = 'Reinstalling Docker Desktop recreates everything.'

    def extra_paths(self, ctx: Context) -> List[Path]:
        return links_into(ctx, ['/usr/local/bin'], lambda target: '/Docker.app/' in target)

    def detect(self, ctx: Context) -> Detection:
        det = super().detect(ctx)
        if ctx.sys('/Applications/Docker.app').exists():
            # The VM is already counted in the size of the previous step; do not count it twice.
            vm = 'path:' + str(ctx.user('~/Library/Containers/com.docker.docker'))
            for item in det.items:
                if item.key == vm:
                    item.size = None
                    item.note = 'size counted in "Uninstall Docker Desktop"'
        return det


STEPS = [DockerDesktop, DockerLeftovers]
