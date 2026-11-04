""".NET, Mono and Xamarin: full removal."""
from __future__ import annotations

from pathlib import Path
from typing import List

from ..core import Context
from ..paths import PathsStep, links_into


class DotnetPkg(PathsStep):
    id = 'dotnet-pkg'
    group = 'dotnet'
    title = 'Remove the pkg-installed .NET'
    targets = ('/usr/local/share/dotnet', '/etc/dotnet', '/etc/paths.d/dotnet', '/etc/paths.d/dotnet-cli-tools')
    receipts = r'^com\.microsoft\.dotnet'
    what = ('Deletes the "native" .NET install in /usr/local/share/dotnet (SDKs 7 and 8 and runtimes), its '
            'PATH entries (/etc/paths.d) and the installer receipts, including those of versions already deleted.')
    why = 'You decided to remove .NET completely.'
    consequences = 'The dotnet command goes away. .NET projects only build again after you reinstall an SDK.'
    undo = 'mise use dotnet@<version> in the project (or -g), when you need it.'

    def extra_paths(self, ctx: Context) -> List[Path]:
        return links_into(ctx, ['/usr/local/bin'], lambda target: '/share/dotnet/' in target)


class MonoXamarin(PathsStep):
    id = 'mono-xamarin'
    group = 'dotnet'
    title = 'Remove Mono and Xamarin'
    targets = ('/Library/Frameworks/Mono.framework', '/Library/Frameworks/Xamarin.Android.framework',
               '/Library/Frameworks/Xamarin.iOS.framework', '/Library/Frameworks/Xamarin.Mac.framework',
               '/Library/Frameworks/Xamarin.Interactive.framework', '/Library/Developer/Xamarin',
               '/etc/paths.d/mono-commands',
               '~/.mono', '~/Library/Xamarin', '~/Library/Developer/Xamarin', '~/Library/Caches/Xamarin',
               '~/Library/Logs/Xamarin*', '~/Library/Preferences/Xamarin')
    receipts = r'^com\.xamarin\.'
    what = ('Deletes Mono 6.12 and the Xamarin frameworks (Android, iOS, Mac), the PATH entry, the links in '
            '/usr/local/bin and the installer receipts.')
    why = 'Tools from Visual Studio for Mac, which is discontinued. They take up about 5.7 GB.'
    consequences = 'The mono command, the old msbuild and similar commands go away.'
    undo = 'There is no official way to reinstall Xamarin; its successor is .NET MAUI.'

    def extra_paths(self, ctx: Context) -> List[Path]:
        return links_into(ctx, ['/usr/local/bin'],
                          lambda target: 'Mono.framework' in target or 'Xamarin.' in target)


class DotnetBrew(PathsStep):
    id = 'dotnet-brew'
    group = 'dotnet'
    title = 'Uninstall .NET from Homebrew'
    formulas = ('dotnet', 'dotnet@8', 'dotnet@6', 'dotnet@9', 'mono-libgdiplus', 'mono')
    casks = ('dotnet-sdk', 'dotnet')
    what = 'Uninstalls dotnet (SDK 10), dotnet@8 and mono-libgdiplus from Homebrew.'
    why = 'You decided to remove .NET completely.'
    consequences = "Homebrew's dotnet goes away."
    undo = 'mise use dotnet@<version>, when you need it.'


class DotnetUser(PathsStep):
    id = 'dotnet-user'
    group = 'dotnet'
    title = 'Remove user .NET data'
    targets = ('~/.dotnet', '~/.nuget', '~/.templateengine', '~/.omnisharp', '~/.local/share/NuGet',
               '~/Library/Caches/NuGet')
    what = ('Deletes ~/.dotnet (global tools such as dotnet-lambda, sentinel files), the NuGet package cache '
            'and the templates.')
    why = 'Without an SDK installed, this data serves no purpose.'
    consequences = 'Global dotnet tools go away; NuGet packages are downloaded again once there is an SDK.'
    undo = 'dotnet tool install -g <tool>, after reinstalling an SDK.'


STEPS = [DotnetPkg, MonoXamarin, DotnetBrew, DotnetUser]
