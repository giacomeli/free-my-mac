"""Ações explícitas de manutenção; nunca executadas durante o inventário."""
import json
import os
from pathlib import Path
import shlex
import signal
import shutil
import stat
import subprocess
import sys
import threading
import time

from dev_audit import inside, run


def file_action(path, policy):
    """Allowlist recalculada na execução. Retorna consequência, não prova de desuso."""
    p = Path(path)
    if not p.is_absolute() or policy.protected(p) or policy.linked(p) or not p.exists():
        return None
    h = policy.home
    age = time.time() - p.stat().st_mtime
    if p.parent == h / 'Downloads' and p.is_file() and p.suffix.lower() in {'.dmg', '.pkg', '.iso'}:
        return 'Instalador antigo; download necessário para reinstalar.' if age >= policy.days * 86400 else None
    if p.parent == h / 'Library/Fonts' and p.is_file() and p.suffix.lower() in {'.ttf', '.otf', '.ttc', '.dfont'}:
        return 'Fonte selecionada manualmente; documentos podem mudar de aparência. Preserve as famílias usadas.'
    if inside(p, h / 'Library/Logs') and p.is_file() and age >= policy.days * 86400:
        return 'Log antigo selecionado; o histórico será perdido. Processo escritor deve estar fechado.'
    if p.parent in {h / 'Library/Developer/Xcode/DerivedData', h / 'Library/Developer/Xcode/Archives'} and p.is_dir():
        return 'Dados Xcode: Archives podem conter dSYMs e builds não reproduzíveis; remoção permanente explícita.'
    if inside(p, policy.projects) and p.is_dir():
        manifests = {'target': ('Cargo.toml', 'pom.xml'), '.next': ('package.json',),
                     'dist': ('package.json',), 'build': ('build.gradle', 'build.gradle.kts', 'CMakeLists.txt'),
                     'bin': (), 'obj': ()}
        known = p.name in manifests and any((p.parent / f).is_file() for f in manifests[p.name])
        if p.name in ('bin', 'obj'):
            known = any(p.parent.glob('*.csproj')) or any(p.parent.glob('*.fsproj'))
        if not known:
            return None
        if any((a / '.git').exists() for a in [p.parent, *p.parent.parents]):
            tracked = run(['/usr/bin/git', '-c', 'core.fsmonitor=false', '--literal-pathspecs',
                           '-C', str(p.parent), 'ls-files', '--', p.name])
            if not tracked['ok'] or tracked['output'].strip():
                return None
        return 'Build selecionado explicitamente, independentemente da idade; fontes preservadas. Feche compiladores.'
    return None


def command_catalog():
    specs = [
        ('ai-install', 'Apple Intelligence: instalar RemoveMacAI via Homebrew', 'brew', ['install', 'omlahore/tap/removemacai'], True, 'Instala dependência externa; não remove modelos nesta etapa.'),
        ('ai-status', 'Apple Intelligence: estado e modelos', 'removemacai', ['status'], False, 'Consulta de modelos e recursos.'),
        ('ai-remove', 'Apple Intelligence: desativar e remover modelos', 'removemacai', ['off'], True, 'Remove modelos via serviço Apple. Aprove o perfil nos Ajustes para impedir novos downloads; conclusão pode ser assíncrona.'),
        ('ai-restore', 'Apple Intelligence: reativar', 'removemacai', ['on'], True, 'Permite usar e baixar novamente os modelos; confira o perfil nos Ajustes.'),
        ('brew-clean', 'Homebrew: downloads e versões antigas', 'brew', ['cleanup', '--prune=all'], True, 'Remove caches e versões antigas; não desinstala todas as versões atuais.'),
        ('brew-orphans', 'Homebrew: dependências sem dependentes', 'brew', ['autoremove'], True, 'Remove fórmulas sem dependentes. Desuso real não é inferido pela idade.'),
        ('npm-cache', 'npm: limpar cache', 'npm', ['cache', 'clean', '--force'], True, 'Novos downloads necessários; npm permanece instalado.'),
        ('pip-cache', 'pip: limpar cache', 'python3', ['-m', 'pip', 'cache', 'purge'], True, 'Somente o cache do pip associado a este Python.'),
        ('uv-cache', 'uv: limpar cache', 'uv', ['cache', 'clean'], True, 'Mantém instalações; cache precisará ser reconstruído.'),
        ('pnpm-cache', 'pnpm: podar store', 'pnpm', ['store', 'prune'], True, 'Remove pacotes não referenciados do store; não remove node_modules.'),
        ('yarn-cache', 'Yarn: limpar cache', 'yarn', ['cache', 'clean'], True, 'Executado fora dos projetos.'),
        ('conda-cache', 'Conda: pacotes e caches sem uso', 'conda', ['clean', '--all', '--yes'], True, 'Preserva ambientes; confira ambientes que usam links para caches.'),
        ('composer-cache', 'Composer: limpar cache', 'composer', ['clear-cache'], True, 'PHP e Composer permanecem instalados.'),
        ('dotnet-cache', '.NET: limpar caches NuGet', 'dotnet', ['nuget', 'locals', 'all', '--clear'], True, 'Pacotes precisarão ser restaurados.'),
        ('go-cache', 'Go: limpar cache de compilação e testes', 'go', ['clean', '-cache', '-testcache'], True, 'Não remove módulos nem Go.'),
        ('docker-images', 'Docker: imagens sem containers associados', 'docker', ['image', 'prune', '-a', '-f'], True, 'Inclui imagens locais sem cópia em registry; serão perdidas. Volumes preservados.'),
        ('docker-build', 'Docker: cache de build não usado', 'docker', ['builder', 'prune', '-a', '-f'], True, 'Próximos builds podem ficar lentos. Volumes preservados.'),
        ('docker-containers', 'Docker: containers parados', 'docker', ['container', 'prune', '-f'], True, 'Perde dados da camada gravável dos containers parados. Volumes preservados.'),
        ('sim-unavailable', 'Apple: simuladores indisponíveis', 'xcrun', ['simctl', 'delete', 'unavailable'], True, 'Exclui dispositivos cujo runtime não está disponível e seus dados.'),
        ('sim-list', 'Apple: listar dispositivos e runtimes', 'xcrun', ['simctl', 'list'], False, 'Consulta somente leitura.'),
        ('cowork', 'Claude Cowork: desativar e remover VM', None, [], True, 'Preserva Claude Desktop, MCP e sessões fora de vm_bundles. Feche o Desktop.'),
    ]
    result = []
    for key, title, tool, args, destructive, impact in specs:
        executable = shutil.which(tool) if tool else sys.executable
        argv = [executable, *args] if executable else []
        if key == 'cowork':
            argv = [sys.executable, str(Path(__file__).with_name('free_my_mac.py')), '--cowork', 'disable', '--apply']
        result.append(dict(id=key, title=title, tool=tool, argv=argv,
                           destructive=destructive, impact=impact, available=bool(executable)))
    return result


def docker_endpoint(executable):
    if any(os.environ.get(k) for k in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')):
        raise ValueError('Remova overrides Docker antes de escolher o daemon local.')
    answer = run([executable, 'context', 'inspect'])
    if not answer['ok']:
        raise ValueError('Não foi possível consultar contexto Docker: ' + answer['error'])
    try:
        endpoint = json.loads(answer['output'])[0]['Endpoints']['docker']['Host']
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError('Contexto Docker inválido') from exc
    if not endpoint.startswith('unix://'):
        raise ValueError('Ações Docker exigem socket Unix local.')
    p = Path(endpoint[7:])
    if not stat.S_ISSOCK(p.stat().st_mode):
        raise ValueError('Endpoint Docker não é socket.')
    return endpoint


def check_native_protections(action_id, argv, protected):
    """Verifica o destino configurado dos caches antes de delegar ao gestor."""
    from dev_audit import Policy
    policy = Policy(Path.home(), protected)
    if action_id == 'ai-remove':
        for root in ('/System/Library/AssetsV2', '/Library/Apple/System/Library/AssetsV2'):
            if policy.protected(Path(root)):
                raise ValueError('Modelos Apple abrangidos por --protect; remoção bloqueada.')
    probes = {
        'npm-cache': ['config', 'get', 'cache'], 'pip-cache': ['-m', 'pip', 'cache', 'dir'],
        'uv-cache': ['cache', 'dir'], 'pnpm-cache': ['store', 'path'],
        'yarn-cache': ['cache', 'dir'], 'composer-cache': ['config', 'cache-dir', '--absolute'],
        'go-cache': ['env', 'GOCACHE'], 'brew-clean': ['--cache'],
    }
    paths = []
    if action_id in probes:
        result = run([argv[0], *probes[action_id]], cwd=Path.home())
        if not result['ok']:
            raise ValueError('Não foi possível validar o destino do cache: ' + result['error'])
        paths = [Path(line.strip()) for line in result['output'].splitlines() if line.strip().startswith('/')]
        if not paths:
            raise ValueError('Destino de cache desconhecido; use seleção de arquivos na auditoria.')
    elif action_id == 'conda-cache':
        result = run([argv[0], 'info', '--json'], cwd=Path.home())
        if not result['ok']:
            raise ValueError('Não foi possível consultar destinos Conda.')
        paths = [Path(p) for p in json.loads(result['output'])['pkgs_dirs']]
    elif action_id == 'dotnet-cache':
        result = run([argv[0], 'nuget', 'locals', 'all', '--list'], cwd=Path.home())
        if not result['ok']:
            raise ValueError('Não foi possível consultar destinos NuGet.')
        paths = [Path(line.split(':', 1)[1].strip()) for line in result['output'].splitlines() if ': /' in line]
        if not paths:
            raise ValueError('Destinos NuGet desconhecidos.')
    for p in paths:
        check_tree_protections(p, policy)
    if action_id in ('brew-clean', 'brew-orphans'):
        result = run([argv[0], '--prefix'], cwd=Path.home())
        if not result['ok'] or not result['output'].strip().startswith('/'):
            raise ValueError('Prefixo Homebrew desconhecido.')
        prefix = Path(result['output'].strip())
        for p in (prefix / 'Cellar', prefix / 'Caskroom'):
            check_tree_protections(p, policy)
    if action_id == 'sim-unavailable':
        check_tree_protections(Path.home() / 'Library/Developer/CoreSimulator/Devices', policy)


def check_tree_protections(path, policy):
    p = Path(path).expanduser()
    if not p.is_absolute() or policy.protected(p) or policy.linked(p):
        raise ValueError('Destino protegido ou link: ' + str(p))
    if not p.exists():
        return
    def failed(exc):
        raise exc
    last = time.monotonic()
    for folder, dirs, files in os.walk(p, followlinks=False, onerror=failed):
        if time.monotonic() - last > 1:
            from free_my_mac import safe_text
            print('Validando proteções: ' + safe_text(folder), flush=True)
            last = time.monotonic()
        for name in dirs + files:
            child = Path(folder) / name
            if policy.protected(child):
                raise ValueError('Destino contém dados protegidos: ' + str(child))


def live_command(argv, timeout=1800, cwd=None):
    """Saída/entrada nativas no terminal, com heartbeat e limite total."""
    from free_my_mac import safe_text
    print('$ ' + safe_text(shlex.join(argv)), flush=True)
    started = time.monotonic()
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(5):
            print(f'\n[em execução: {time.monotonic() - started:.0f}s | Ctrl-C cancela]', file=sys.stderr, flush=True)
    ticker = threading.Thread(target=heartbeat, daemon=True)
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE='1', HOMEBREW_NO_ANALYTICS='1')
    # Grupo próprio na mesma sessão para permitir entrada interativa no terminal.
    process = subprocess.Popen(argv, cwd=cwd, env=env, preexec_fn=os.setpgrp)
    terminal = None
    previous_group = None
    old_ttou = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    if sys.stdin.isatty():
        terminal = sys.stdin.fileno()
        previous_group = os.tcgetpgrp(terminal)
        os.tcsetpgrp(terminal, process.pid)
        # O filho pode ter lido stdin antes de receber o terminal e parado em SIGTTIN.
        try:
            os.killpg(process.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
    ticker.start()
    try:
        return process.wait(timeout=timeout)
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if terminal is not None and previous_group is not None:
            os.tcsetpgrp(terminal, previous_group)
        signal.signal(signal.SIGTTOU, old_ttou)
        stop.set()
        ticker.join()


def perform(action_id, apply=False, protected=()):
    if os.geteuid() == 0:
        raise ValueError('Execute sem sudo; os gerenciadores usam seu usuário.')
    choices = {a['id']: a for a in command_catalog()}
    if action_id not in choices:
        raise ValueError('Ação desconhecida: ' + action_id)
    action = choices[action_id]
    if not action['available']:
        extra = ' Instale com: brew install omlahore/tap/removemacai' if action['tool'] == 'removemacai' else ''
        raise ValueError(f"Ferramenta ausente: {action['tool']}." + extra)
    argv = list(action['argv'])
    if action_id == 'cowork':
        for path in protected:
            argv += ['--protect', str(path)]
    if action_id.startswith('docker-'):
        endpoint = docker_endpoint(argv[0])
        argv[1:1] = ['--host', endpoint]
        print('Daemon: ' + endpoint + '\nUm socket local pode encaminhar a outro host; confirme que é seu Docker local.')
    print(action['title'] + '\n' + action['impact'] + '\n' + shlex.join(argv))
    if action['destructive'] and not apply:
        print('SIMULAÇÃO: nenhuma alteração. Use --apply para executar com confirmação.')
        return 0
    if action['destructive']:
        if not sys.stdin.isatty():
            raise ValueError('Execução exige terminal interativo.')
        check_native_protections(action_id, argv, protected)
        if action_id == 'brew-orphans':
            if live_command([argv[0], 'autoremove', '--dry-run']):
                raise ValueError('Prévia Homebrew falhou.')
        if action_id == 'brew-clean':
            if live_command([argv[0], 'cleanup', '--prune=all', '--dry-run']):
                raise ValueError('Prévia Homebrew falhou.')
        if input('Digite EXECUTAR para aplicar esta ação: ') != 'EXECUTAR':
            return 0
        check_native_protections(action_id, argv, protected)
    from dev_audit import save_new
    stamp = time.time_ns()
    log = Path.cwd() / f'free-my-mac-action-{stamp}.json'
    before = shutil.disk_usage(Path.home()).free
    event = dict(id=action_id, argv=argv, started=time.time(), free_before=before,
                 status='iniciando')
    save_new(log, event)
    code = None
    try:
        # Mesma configuração usada na consulta dos destinos de cache.
        code = live_command(argv, cwd=Path.home())
        event['status'] = 'comando concluído' if code == 0 else 'falha; alterações parciais possíveis'
        return code
    except BaseException:
        event['status'] = 'interrompido; alterações parciais possíveis'
        raise
    finally:
        event.update(returncode=code, ended=time.time(), free_after=shutil.disk_usage(Path.home()).free)
        # Arquivo criado por nós com 0600; atualização por fd, sem seguir links.
        fd = os.open(log, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
        with os.fdopen(fd, 'w') as stream:
            json.dump(event, stream, indent=2)
        print(f'Registro: {log}')


def menu(protected=(), policy=None):
    from fzf_ui import choose
    if not shutil.which('fzf'):
        raise ValueError('fzf ausente; use --maintenance list.')
    while True:
        actions = command_catalog()
        rows = '\n'.join(f"{i}\t{a['title']} {'[ferramenta ausente]' if not a['available'] else ''}" for i, a in enumerate(actions))
        rows += f'\n{len(actions)}\tRemover versões, apps Homebrew e simuladores individualmente'
        picked = choose(rows, 'MANUTENÇÃO | escolha uma ação para ver consequências; Esc volta')
        if not picked:
            return
        try:
            if picked[0] == len(actions):
                from dev_audit import Policy
                from managed_cleanup import menu as managed_menu
                managed_menu(policy or Policy(Path.home(), protected))
            else:
                perform(actions[picked[0]]['id'], apply=True, protected=protected)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f'Falha: {exc}')
        except KeyboardInterrupt:
            print('\nInterrompido; confira o registro antes de repetir.')
        input('Enter para voltar ao menu...')
