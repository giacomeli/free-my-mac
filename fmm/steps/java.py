"""Java: Temurin 21 via mise and removal of standalone JDKs."""
from __future__ import annotations

from typing import List

from ..core import CLEAN, ERROR, PENDING, Context, Detection, Item, Op, Step
from ..paths import PathsStep
from .mise import global_tools


class JavaMise(Step):
    id = 'java-mise'
    group = 'java'
    title = 'Java 21 (Temurin) via mise'
    requires = ('mise-config',)
    what = 'Sets Temurin 21 as the global Java in mise; mise also takes over setting JAVA_HOME.'
    why = 'Today there are 9 JDKs from 5 sources, and JAVA_HOME is hardcoded in .zshenv and .zshrc.'
    consequences = 'Projects that need another version run mise use java@<version> in their folder.'
    undo = 'mise unuse -g java.'

    def detect(self, ctx: Context) -> Detection:
        if not ctx.tool('mise'):
            return Detection(ERROR, summary='mise not found.')
        if 'java' in global_tools(ctx):
            return Detection(CLEAN, summary='Global Java in mise: ' + global_tools(ctx)['java'])
        return Detection(PENDING, [Item('java', 'mise use -g java@temurin-21')])

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        return [Op('Set the global Java in mise', 'mise use -g java@temurin-21',
                   argv=[ctx.tool('mise') or 'mise', 'use', '-g', 'java@temurin-21'])]


class JavaOld(PathsStep):
    id = 'java-old'
    group = 'java'
    title = 'Remove standalone JDKs'
    requires = ('java-mise',)
    formulas = ('openjdk@17', 'openjdk@21')
    casks = ('liberica-jdk20-full',)
    taps = ('bell-sw/liberica',)
    tolerate_brew_failure = True
    targets = ('/Library/Java/JavaVirtualMachines/*', '/Library/Internet Plug-Ins/JavaAppletPlugin.plugin',
               '/Library/PreferencePanes/JavaControlPanel.prefPane', '/opt/homebrew/Caskroom/liberica-jdk20-full')
    what = ('Deletes the JDKs in /Library/Java (Oracle 23, Liberica 20, Microsoft 11, Temurin 17 and 8, and the '
            'openjdk links), the Java 8 browser plugin, and openjdk@17 and openjdk@21 from Homebrew.')
    why = 'Three of them are x86_64 (they run through Rosetta). With Temurin 21 in mise, none of them is needed.'
    consequences = ("Homebrew's openjdk (25) stays, because ant, gradle and sleuthkit depend on it. The JetBrains "
                    'JBR in ~/Library/Java is not touched. The openjdk@21 lines in the shell files are removed in '
                    'the Shell step.')
    undo = 'mise use java@<version> for any version a project asks for.'


STEPS = [JavaMise, JavaOld]
