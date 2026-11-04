"""Catalog: groups in the recommended run order."""
from __future__ import annotations

from typing import List, Tuple

from ..core import Step

GROUPS: List[Tuple[str, str]] = [
    ('prep', 'Preparation'),
    ('mise', 'mise: one version manager'),
    ('python', 'Python'),
    ('php', 'PHP and Herd'),
    ('docker', 'Docker'),
    ('dotnet', '.NET, Mono and Xamarin'),
    ('sim', 'iOS simulators'),
    ('node', 'Node'),
    ('java', 'Java'),
    ('general', 'General cleanup'),
    ('leftovers', 'Leftovers of removed apps'),
    ('apps', 'Fonts and Chrome AI'),
    ('devtools', 'Unused tools and databases'),
    ('artifacts', 'Project artifacts'),
    ('shell', 'Shell'),
    ('maint', 'Maintenance'),
]

# Tweaks catalog (sh tweaks.sh): reversible debloat and optimization tweaks.
TWEAK_GROUPS: List[Tuple[str, str]] = [
    ('rmai', 'RemoveMacAI: privacy and interface'),
    ('background', 'Background items'),
    ('tuning', 'Animations, firewall and energy'),
]

GROUP_TITLES = dict(GROUPS + TWEAK_GROUPS)
CATALOG_TITLES = {'cleanup': 'FREE MY MAC', 'tweaks': 'FREE MY MAC | TWEAKS'}


def _build(catalog: str) -> List[Step]:
    if catalog == 'tweaks':
        from . import rmai, tweaks
        modules, groups = [rmai, tweaks], TWEAK_GROUPS
    else:
        from . import (apps, artifacts, devtools, docker, dotnet, general, java, leftovers, maintenance, mise,
                       node, php, prep, python, shell, simulators)
        modules = [prep, mise, python, php, docker, dotnet, simulators, node, java, general, leftovers, apps,
                   devtools, artifacts, shell, maintenance]
        groups = GROUPS
    steps: List[Step] = []
    for module in modules:
        steps.extend(cls() for cls in module.STEPS)
    order = {g: i for i, (g, _) in enumerate(groups)}
    steps.sort(key=lambda s: order[s.group])
    return steps


def all_steps(catalog: str = 'cleanup') -> List[Step]:
    if catalog not in CATALOG_TITLES:
        raise ValueError('unknown catalog: ' + catalog)
    steps = _build(catalog)
    ids = [s.id for s in steps]
    other = [s.id for s in _build('cleanup' if catalog == 'tweaks' else 'tweaks')]
    # History is a single file: ids must be unique across both catalogs.
    if len(ids) != len(set(ids)) or set(ids) & set(other):
        raise RuntimeError('duplicate step ids in the catalog')
    for index, step in enumerate(steps):
        for required in step.requires:
            if required not in ids[:index]:
                raise RuntimeError('step %s depends on %s, which does not come before it' % (step.id, required))
    return steps
