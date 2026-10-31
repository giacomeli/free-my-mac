"""Remoção individual por gerenciadores; não infere desuso a partir de idade."""
import json
import os
from pathlib import Path
import re
import shutil
import sys

from dev_audit import run


KINDS = {
    'brew-formula': 'Homebrew: fórmulas (inclui PHP, .NET e Python)',
    'brew-cask': 'Homebrew: aplicativos e fontes',
    'pyenv': 'Python: versões pyenv', 'conda': 'Python: ambientes Conda/Miniforge',
    'nvm': 'Node: versões nvm', 'fnm': 'Node: versões fnm',
    'asdf': 'asdf: versões instaladas', 'mise': 'mise: versões instaladas',
    'rustup': 'Rust: toolchains', 'android': 'Android: pacotes SDK e imagens',
    'avd': 'Android: dispositivos virtuais', 'simulator': 'Apple: dispositivos simulados',
    'runtime': 'Apple: imagens de runtime',
}


def query(argv):
    print('Consultando: ' + ' '.join(argv) + ' (limite 15s)', flush=True)
    answer = run(argv, cwd=Path.home())
    if not answer['ok']:
        raise ValueError(answer['error'] or 'Consulta falhou')
    return answer['output']


def tool(name):
    executable = shutil.which(name)
    if not executable and name in ('sdkmanager', 'avdmanager'):
        candidate = Path.home() / 'Library/Android/sdk/cmdline-tools/latest/bin' / name
        if candidate.is_file():
            executable = str(candidate)
    if not executable:
        raise ValueError(f'{name} não encontrado; inclua o executável no PATH.')
    return executable


def candidates(kind, home=None):
    home = Path(home or Path.home())
    result = []
    def add(label, argv, identity=None, path=None):
        result.append(dict(label=label, argv=argv, identity=identity or label, path=str(path) if path else None))
    if kind.startswith('brew-'):
        exe = tool('brew')
        flag = '--formula' if kind == 'brew-formula' else '--cask'
        for name in query([exe, 'list', flag, '-1']).splitlines():
            if re.fullmatch(r'[A-Za-z0-9@+_. /-]+', name) and not name.startswith('-'):
                add(name, [exe, 'uninstall', flag, name])
    elif kind == 'pyenv':
        exe = tool('pyenv')
        root = Path(query([exe, 'root']).strip())
        active = query([exe, 'version-name']).strip().split(':')
        for name in query([exe, 'versions', '--bare']).splitlines():
            if name not in active and name != 'system' and re.fullmatch(r'[\w.+-]+', name) and not name.startswith('-'):
                add(name, [exe, 'uninstall', '-f', name], path=root / 'versions' / name)
    elif kind == 'conda':
        executables = [shutil.which('conda')]
        executables += [str(home / root / 'bin/conda') for root in ('miniforge3', 'miniconda3', 'anaconda3') if (home / root / 'bin/conda').is_file()]
        for exe in dict.fromkeys(e for e in executables if e):
            info = json.loads(query([exe, 'info', '--json']))
            for prefix in json.loads(query([exe, 'env', 'list', '--json'])).get('envs', []):
                if prefix not in (info.get('root_prefix'), info.get('active_prefix')):
                    add(f'{exe}: {prefix}', [exe, 'env', 'remove', '--prefix', prefix, '--yes'], path=prefix)
    elif kind in ('nvm', 'fnm'):
        if kind == 'nvm':
            root = Path(os.environ.get('NVM_DIR', str(home / '.nvm'))).expanduser()
            script = root / 'nvm.sh'
            if not script.is_file():
                raise ValueError('nvm.sh ausente; configure NVM_DIR.')
            for p in sorted((root / 'versions/node').glob('v*')):
                if p.is_dir() and re.fullmatch(r'v\d+\.\d+\.\d+', p.name):
                    # nvm é função; argumentos são posicionais, nunca interpolados no shell.
                    add(p.name, ['/bin/bash', '--noprofile', '--norc', '-c', '. "$1" --no-use && nvm uninstall "$2"', 'free-my-mac', str(script), p.name], path=p)
        else:
            exe = tool('fnm')
            root = Path(json.loads(query([exe, 'env', '--json']))['FNM_DIR'])
            for line in query([exe, 'list']).splitlines():
                clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
                match = re.search(r'\bv\d+\.\d+\.\d+\b', clean)
                if match and '*' not in clean and 'default' not in clean:
                    add(match[0], [exe, 'uninstall', match[0]], path=root / 'node-versions' / match[0])
    elif kind in ('asdf', 'mise'):
        exe = tool(kind)
        root = Path(os.environ.get('ASDF_DATA_DIR', str(home / '.asdf'))) / 'installs' if kind == 'asdf' else Path(os.environ.get('MISE_DATA_DIR', str(home / '.local/share/mise'))) / 'installs'
        for package in sorted(root.glob('*')):
            for version in sorted(package.glob('*')):
                if version.is_dir() and not version.is_symlink() and all(re.fullmatch(r'[A-Za-z0-9_.+-]+', n) and not n.startswith('-') for n in (package.name, version.name)):
                    args = [package.name, version.name] if kind == 'asdf' else [package.name + '@' + version.name]
                    add(package.name + '@' + version.name, [exe, 'uninstall', *args], path=version)
    elif kind == 'rustup':
        exe = tool('rustup')
        for line in query([exe, 'toolchain', 'list']).splitlines():
            if line and '(active' not in line and '(default' not in line:
                name = line.split()[0]
                if re.fullmatch(r'[\w.+-]+', name) and not name.startswith('-'):
                    add(name, [exe, 'toolchain', 'uninstall', name], path=Path(os.environ.get('RUSTUP_HOME', str(home / '.rustup'))) / 'toolchains' / name)
    elif kind == 'android':
        exe = tool('sdkmanager')
        root = Path(os.environ.get('ANDROID_SDK_ROOT', os.environ.get('ANDROID_HOME', str(home / 'Library/Android/sdk'))))
        for line in query([exe, '--sdk_root=' + str(root), '--list_installed']).splitlines():
            name = line.split('|')[0].strip()
            if name.startswith(('system-images;', 'platforms;', 'build-tools;')) and re.fullmatch(r'[A-Za-z0-9_;.+-]+', name):
                add(name, [exe, '--sdk_root=' + str(root), '--uninstall', name], path=root.joinpath(*name.split(';')))
    elif kind == 'avd':
        exe = tool('avdmanager')
        root = Path(os.environ.get('ANDROID_AVD_HOME', str(Path(os.environ.get('ANDROID_USER_HOME', str(home / '.android'))) / 'avd')))
        for name in query([exe, 'list', 'avd', '-c']).splitlines():
            if re.fullmatch(r'[A-Za-z0-9_.-]+', name) and not name.startswith('-'):
                add(name, [exe, 'delete', 'avd', '-n', name], path=root / (name + '.avd'))
    elif kind == 'simulator':
        exe = tool('xcrun')
        data = json.loads(query([exe, 'simctl', 'list', 'devices', '--json']))
        for runtime, devices in data['devices'].items():
            for d in devices:
                if d.get('state') == 'Shutdown' and re.fullmatch(r'[A-Fa-f0-9-]{36}', d['udid']):
                    add(f"{d['name']} | {runtime} | {d['udid']}", [exe, 'simctl', 'delete', d['udid']], d['udid'], home / 'Library/Developer/CoreSimulator/Devices' / d['udid'])
    elif kind == 'runtime':
        exe = tool('xcrun')
        data = json.loads(query([exe, 'simctl', 'runtime', 'list', '--json']))
        records = data.values() if isinstance(data, dict) else data
        for d in records:
            identifier = d.get('identifier', '')
            if re.fullmatch(r'[A-Fa-f0-9-]{36}', identifier) and d.get('deletable', True):
                paths = [d[k] for k in ('parentImagePath', 'path', 'mountPath') if d.get(k)]
                if not paths:
                    continue
                add(f"{d.get('version', '')} {d.get('build', '')} | {identifier}", [exe, 'simctl', 'runtime', 'delete', identifier], identifier, paths[0])
                result[-1]['additional_paths'] = paths[1:]
    else:
        raise ValueError('Gerenciador desconhecido')
    return result


def check_selected(kind, selected, policy):
    from maintenance import check_tree_protections
    for item in selected:
        paths = [Path(item['path'])] if item.get('path') else []
        paths += [Path(p) for p in item.get('additional_paths', [])]
        if kind == 'brew-formula':
            root = Path(query([item['argv'][0], '--cellar']).strip())
            paths.append(root / item['argv'][-1])
        if kind == 'brew-cask':
            exe, name = item['argv'][0], item['argv'][-1]
            root = Path(query([exe, '--caskroom']).strip())
            paths.append(root / name)
            data = json.loads(query([exe, 'info', '--cask', '--json=v2', name]))['casks'][0]
            if os.environ.get('HOMEBREW_CASK_OPTS'):
                raise ValueError('HOMEBREW_CASK_OPTS customizado: destinos de casks não verificados.')
            for artifact in data['artifacts']:
                if 'app' in artifact:
                    app = artifact['app'][0]
                    target = next((a['target'] for a in artifact['app'] if isinstance(a, dict) and 'target' in a), Path(app).name)
                    paths.append(Path('/Applications') / target)
                if 'font' in artifact:
                    paths.append(policy.home / 'Library/Fonts' / Path(artifact['font'][0]).name)
                if any(k in artifact for k in ('pkg', 'installer')):
                    raise ValueError('Cask com instalador externo: destinos não verificáveis. Use seu desinstalador específico.')
        if not paths:
            raise ValueError('Destinos de remoção desconhecidos; operação bloqueada.')
        for path in paths:
            check_tree_protections(path, policy)


def menu(policy):
    from fzf_ui import choose
    from maintenance import live_command
    from dev_audit import project_requirements, save_new
    import tempfile
    import time
    if os.geteuid() == 0:
        raise ValueError('Use os gerenciadores sem sudo.')
    kinds = list(KINDS)
    picked = choose('\n'.join(f'{i}\t{KINDS[k]}' for i, k in enumerate(kinds)), 'Remoção individual | versões, pacotes e simuladores')
    if not picked:
        return
    kind = kinds[picked[0]]
    options = candidates(kind)
    if not options:
        print('Nenhum candidato removível encontrado.')
        return
    selection = choose('\n'.join(f"{i}\t{a['label']}" for i, a in enumerate(options)), 'Seleção manual: idade NÃO comprova desuso. Feche apps, emuladores e builds.', multi=True)
    if not selection:
        return
    print('Requisitos encontrados em projetos (revise dependências antes de remover versões):')
    print(json.dumps(project_requirements(policy.projects, policy), ensure_ascii=False, indent=2))
    import shlex
    selected = [options[i] for i in selection]
    check_selected(kind, selected, policy)
    for item in selected:
        print(item['label'] + '\n  ' + shlex.join(item['argv']))
    print('Ambientes podem conter dados e pacotes locais. Simuladores perdem seus dados. Sem rollback automático.')
    if kind == 'runtime':
        print('A remoção nativa de runtime pode desligar simuladores que o estejam usando.')
    if input('Digite REMOVER SELECIONADOS para executar: ') != 'REMOVER SELECIONADOS':
        return
    # Reconsulta antes de executar; não aceita comandos vindos de arquivo externo.
    fresh = {json.dumps(a['argv']) for a in candidates(kind)}
    if any(json.dumps(a['argv']) not in fresh for a in selected):
        raise ValueError('Inventário mudou; selecione novamente.')
    log = Path.cwd() / f'free-my-mac-managed-{time.time_ns()}.jsonl'
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        def record(event):
            stream.write(json.dumps(event) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        for item in selected:
            check_selected(kind, [item], policy)
            record(dict(item, status='iniciando'))
            try:
                code = live_command(item['argv'], cwd=Path.home())
            except BaseException:
                record(dict(item, status='interrompido; alterações parciais possíveis'))
                raise
            record(dict(item, returncode=code, status='concluído' if not code else 'falha parcial possível'))
            if code:
                break
    print('Registro: ' + str(log))
