"""Auditoria por ecossistema e planos restritos de manutenção."""
from dataclasses import dataclass, asdict
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import time


# Apenas caches isolados; stores e ambientes compartilhados ficam fora do executor.
CACHES = {
    '.npm/_cacache': 'npm', '.cache/pip': 'pip', 'Library/Caches/pip': 'pip',
    'Library/Caches/Homebrew': 'Homebrew', '.gradle/caches': 'Gradle',
    'Library/Caches/composer': 'Composer', '.cache/uv': 'uv',
    'Library/Caches/uv': 'uv', '.cache/yarn': 'Yarn',
}
CATALOG = {
    'Node': ['.nvm/versions/node/*', '.fnm/node-versions/*', '.volta/tools/image/*', '.bun', 'Library/pnpm/store', '.local/share/pnpm/store'],
    'Python': ['.pyenv/versions/*', 'miniforge3', 'miniconda3', 'anaconda3', '.conda/envs/*', '.local/share/uv/python/*', '.local/share/pipx/venvs/*'],
    'Versões': ['.asdf/installs/*/*', '.local/share/mise/installs/*/*', '.rustup/toolchains/*', '.dotnet', '.rbenv/versions/*'],
    'Dependências': ['.cargo/registry', '.m2/repository', '.nuget/packages', 'go/pkg/mod', '.cache/composer', '.bun/install/cache', 'Library/Caches/Yarn'],
    'Apple SDK': ['Library/Developer/Xcode/DerivedData/*', 'Library/Developer/Xcode/Archives/*', 'Library/Developer/CoreSimulator/Devices/*'],
    'Android': ['.android/avd/*', 'Library/Android/sdk/system-images/*/*/*', 'Library/Android/sdk/platforms/*', 'Library/Android/sdk/build-tools/*'],
    'Cowork': ['Library/Application Support/Claude/vm_bundles/*', 'Library/Application Support/Claude/*vm*'],
    'Herd': ['Library/Application Support/Herd/*'],
    'Fontes': ['Library/Fonts/*'],
    'Logs': ['Library/Logs/*'],
    'Instaladores': ['Downloads/*.dmg', 'Downloads/*.pkg', 'Downloads/*.iso'],
    'Apps e resíduos': ['Library/Application Support/*', 'Library/LaunchAgents/*'],
    'Atualizações e caches': ['Library/Caches/*'],
}
SYSTEM = {
    'Instalações': ['/opt/*', '/usr/local/*', '/Library/Frameworks/Python.framework/Versions/*', '/usr/local/share/dotnet/sdk/*', '/usr/local/share/dotnet/shared/*/*', '/opt/homebrew/Cellar/*'],
    'Fontes': ['/Library/Fonts/*'],
    'Apple SDK': ['/Library/Developer/CoreSimulator/Volumes/*', '/Library/Developer/CoreSimulator/Profiles/Runtimes/*'],
    'Logs': ['/Library/Logs/*', '/var/log/*'],
    'Serviços': ['/Library/LaunchAgents/*', '/Library/LaunchDaemons/*'],
    'Apps Apple': ['/System/Applications/*.app'],
}


PROGRESS = lambda message: None


def run(argv, timeout=15, cwd=None):
    PROGRESS('Consulta: ' + ' '.join(map(str, argv)) + f' (limite {timeout}s)')
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE='1', HOMEBREW_NO_ANALYTICS='1',
               GIT_OPTIONAL_LOCKS='0')
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
        return {'ok': p.returncode == 0, 'output': p.stdout[:200000], 'error': p.stderr[:2000]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'ok': False, 'output': '', 'error': str(exc)}


def inside(path, parent):
    return path == parent or parent in path.parents


class Policy:
    def __init__(self, home, protected=(), projects=None, days=60):
        self.home = Path(home).resolve()
        self.projects = Path(projects or self.home / 'Projects').expanduser().resolve()
        self.days = days
        self.roots = [self.home / p for p in ('.claude', '.codex', '.cursor', '.ollama', '.cache/huggingface', 'Library/Application Support/Claude/local-agent-mode-sessions')]
        self.roots += [Path(p).expanduser().resolve() for p in protected]
        self.roots = list(dict.fromkeys(self.roots))

    def protected(self, path):
        path = Path(path).resolve()
        return (any('sketchpad' in part.casefold() for part in path.parts)
                or any(inside(path, p) or inside(p, path) for p in self.roots))

    def linked(self, path):
        return any(p.is_symlink() for p in [path, *path.parents] if inside(p, self.home))

    def skip_scan(self, path):
        path = Path(path).resolve()
        return any('sketchpad' in part.casefold() for part in path.parts) or any(inside(path, p) for p in self.roots)


@dataclass
class Finding:
    category: str
    path: str
    owner: str
    reason: str
    size: object = None
    confidence: str = 'revisão necessária'
    action: str = 'review'
    evidence: str = ''
    identity: object = None


def identity(path):
    s = path.lstat()
    return [s.st_dev, s.st_ino, s.st_mtime_ns, s.st_uid]


def finding(path, category, owner, reason, policy, action='review'):
    if policy.protected(path):
        action, reason = 'protected', 'Preservado por regra de proteção. ' + reason
    elif policy.linked(path):
        action, reason = 'review', 'Link simbólico no caminho; não executar. ' + reason
    try:
        stamp = identity(path)
    except OSError:
        stamp = None
    return Finding(category, str(path), owner, reason, action=action, identity=stamp)


def catalog_findings(policy, system=False):
    from maintenance import file_action
    result, seen = [], set()
    for relative, owner in CACHES.items():
        p = policy.home / relative
        if p.exists():
            result.append(finding(p, 'Caches isolados', owner,
                'Cache reconstruível; fechar ferramentas antes de remover. Requer novos downloads.', policy, 'delete-cache'))
            seen.add(str(p))
    groups = [(category, list(policy.home.glob(pattern))) for category, patterns in CATALOG.items() for pattern in patterns]
    if system:
        groups += [(category, list(Path('/').glob(pattern.lstrip('/')))) for category, patterns in SYSTEM.items() for pattern in patterns]
    for category, paths in groups:
        for p in paths:
            if str(p) in seen:
                continue
            seen.add(str(p))
            consequence = file_action(p, policy)
            result.append(finding(p, category, category,
                consequence or 'Inventário: tamanho/idade não provam desuso; validar dependências e dados antes de remover.', policy,
                'delete-reviewed' if consequence else 'review'))
            if category == 'Logs' and p.is_dir() and not policy.skip_scan(p) and not policy.linked(p):
                for folder, dirs, files in os.walk(p, followlinks=False):
                    dirs[:] = [d for d in dirs if not policy.skip_scan(Path(folder) / d) and not policy.linked(Path(folder) / d)]
                    for name in files:
                        log = Path(folder) / name
                        consequence = file_action(log, policy)
                        if consequence:
                            result.append(finding(log, 'Logs antigos', 'Log', consequence, policy, 'delete-reviewed'))
    return result


OUTPUTS = {'node_modules', '.next', 'dist', 'build', 'target', 'bin', 'obj', '.venv', 'venv', '.git'}


def project_findings(root, policy, days, stop=None):
    result = []
    root = Path(root)
    cutoff = time.time() - days * 86400
    if not root.is_dir() or policy.skip_scan(root) or root.is_symlink():
        return result
    for folder, dirs, files in os.walk(root, followlinks=False):
        if stop and stop.is_set():
            break
        base = Path(folder)
        PROGRESS('Projetos: ' + str(base))
        dirs[:] = [d for d in dirs if d not in OUTPUTS and not policy.skip_scan(base / d) and not (base / d).is_symlink()]
        manifests = set(files) & {'package.json', 'Cargo.toml', 'pyproject.toml', 'pom.xml', 'build.gradle', 'build.gradle.kts', 'CMakeLists.txt', 'composer.json'}
        manifests |= {f for f in files if f.endswith(('.csproj', '.fsproj'))}
        if not manifests:
            continue
        newest, count, errors = 0, 0, []
        def onerror(exc):
            errors.append(str(exc))
        for source, children, names in os.walk(base, followlinks=False, onerror=onerror):
            PROGRESS('Fontes: ' + str(source))
            if any(n not in OUTPUTS and (policy.skip_scan(Path(source) / n) or (Path(source) / n).is_symlink()) for n in children):
                errors.append('fontes protegidas ou links não inspecionados')
            children[:] = [n for n in children if n not in OUTPUTS and not (Path(source) / n).is_symlink() and not policy.skip_scan(Path(source) / n)]
            for name in names:
                p = Path(source) / name
                if p.is_symlink() or policy.skip_scan(p):
                    errors.append('fonte protegida ou link não inspecionado')
                    continue
                try:
                    newest = max(newest, p.stat().st_mtime)
                    count += 1
                except OSError as exc:
                    errors.append(str(exc))
            if count > 50000 or (stop and stop.is_set()):
                errors.append('limite de inspeção')
                break
        lock = bool(set(files) & {'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'bun.lock', 'bun.lockb'})
        state = 'atividade recente' if newest >= cutoff else 'indício de inatividade; uso real desconhecido'
        if errors or not count:
            state = 'atividade desconhecida: inspeção incompleta'
        for name in OUTPUTS - {'.git', '.venv', 'venv'}:
            target = base / name
            if target.is_dir():
                reason = f'{state}; janela de {days} dias; {count} fontes examinadas; lockfile Node: {lock}.'
                f = finding(target, 'Projetos e builds', ', '.join(sorted(manifests)), reason, policy)
                f.evidence = 'Apenas proposta. Git, processos e alterações internas de dependências exigem revisão; nenhuma exclusão automática.'
                from maintenance import file_action
                consequence = file_action(target, policy)
                if consequence:
                    f.action = 'delete-reviewed'
                    f.evidence = consequence
                if name == 'node_modules' and lock and newest < cutoff and not errors and count and f.action != 'protected' and not policy.linked(target):
                    f.action = 'delete-node-modules'
                    f.evidence = 'Revisar antes de selecionar: dependências serão reinstaladas pelo lockfile; alterações manuais dentro de node_modules serão perdidas. Fechar processos do projeto.'
                    if any((ancestor / '.git').exists() for ancestor in [base, *base.parents]):
                        tracked = run(['/usr/bin/git', '-c', 'core.fsmonitor=false', '--literal-pathspecs', '-C', str(base), 'ls-files', '--', 'node_modules'])
                        if not tracked['ok'] or tracked['output'].strip():
                            f.action = 'review'
                            f.evidence += ' Dependências versionadas ou Git indisponível: exclusão bloqueada.'
                result.append(f)
    return result


def project_requirements(root, policy, stop=None):
    names = {'.nvmrc', '.node-version', '.python-version', '.tool-versions', 'mise.toml',
             'global.json', 'rust-toolchain.toml', 'runtime.txt', 'environment.yml'}
    result = {}
    if policy.skip_scan(root) or Path(root).is_symlink():
        return result
    for folder, dirs, files in os.walk(root, followlinks=False):
        if stop and stop.is_set():
            break
        base = Path(folder)
        PROGRESS('Projetos: ' + str(base))
        dirs[:] = [n for n in dirs if n not in OUTPUTS and not policy.skip_scan(base / n) and not (base / n).is_symlink()]
        for name in names.intersection(files):
            p = base / name
            if p.is_symlink():
                continue
            try:
                with p.open() as stream:
                    result[str(p)] = stream.read(8192).strip()
            except (OSError, UnicodeError):
                result[str(p)] = 'leitura indisponível'
    return result


def tool_inventory(stop=None):
    # Não inicializa shells interativos nem carrega configurações de projeto.
    names = 'brew node npm pnpm yarn bun python3 pip pip3 uv conda mamba pyenv pipx nvm fnm volta asdf mise php composer herd dotnet cargo rustup ruby gem go java gradle mvn docker colima orb xcrun adb'.split()
    tools = {name: shutil.which(name) for name in names}
    probes = {}
    for name, args in [('brew', ['list', '--versions']), ('dotnet', ['--list-sdks']), ('rustup', ['toolchain', 'list'])]:
        if stop and stop.is_set():
            break
        if tools[name]:
            probes[name] = run([tools[name], *args])
    if tools['dotnet'] and not (stop and stop.is_set()):
        probes['dotnet-runtimes'] = run([tools['dotnet'], '--list-runtimes'])
    return {'paths': tools, 'probes': probes,
            'note': 'nvm pode ser função de shell; versões em disco são inventariadas separadamente. PATH não cobre todas as instalações.'}


def docker_inventory(stop=None):
    docker = shutil.which('docker')
    if not docker:
        return {'ok': False, 'error': 'Docker CLI ausente; daemon não iniciado.'}
    if os.environ.get('DOCKER_HOST') or os.environ.get('DOCKER_CONTEXT'):
        return {'ok': False, 'error': 'Override de endpoint/contexto: inspeção automática bloqueada.'}
    info = run([docker, 'context', 'inspect'])
    try:
        endpoint = json.loads(info['output'])[0]['Endpoints']['docker']['Host']
    except (KeyError, IndexError, ValueError, TypeError):
        return {'ok': False, 'error': 'Não foi possível identificar endpoint local.'}
    if not endpoint.startswith('unix://') or (stop and stop.is_set()):
        return {'ok': False, 'error': 'Endpoint remoto: não inspecionado.', 'endpoint': endpoint}
    return {'endpoint': endpoint, 'usage': run([docker, 'system', 'df', '-v']),
            'note': 'Volumes e imagens locais preservados. Socket local pode ser encaminhado; nenhuma ação prune é executada.'}


def apple_preflight(stop=None):
    result = {'decision': 'Revisão manual: não há executor para volume selado. SIP desativado não basta; nenhum remount ou snapshot será alterado.'}
    for name, argv in [('version', ['/usr/bin/sw_vers']), ('sip', ['/usr/bin/csrutil', 'status']),
                       ('authenticated_root', ['/usr/bin/csrutil', 'authenticated-root', 'status']), ('mounts', ['/sbin/mount'])]:
        if stop and stop.is_set():
            break
        result[name] = run(argv)
    return result


def audit(policy, projects, days=60, system=False, measure=False, stop=None, probes=True):
    PROGRESS('Inventário de instalações e diretórios')
    items = catalog_findings(policy, system)
    PROGRESS('Análise de projetos')
    items += project_findings(projects, policy, days, stop)
    if measure:
        from free_my_mac import measure as disk_size
        for f in items:
            if stop and stop.is_set():
                break
            if f.action == 'protected' or policy.linked(Path(f.path)):
                continue
            PROGRESS('Medindo: ' + f.path + ' (limite 30s)')
            f.size, status = disk_size(Path(f.path))
            f.evidence += ' Medição: ' + status
    PROGRESS('Lendo requisitos dos projetos')
    requirements = project_requirements(projects, policy, stop)
    from audit_adapters import enrich_findings, brew_inventory
    apps = enrich_findings(items, policy, days, requirements, stop) if not (stop and stop.is_set()) else {}
    tools = tool_inventory(stop) if probes and not (stop and stop.is_set()) else {}
    docker = docker_inventory(stop) if probes and not (stop and stop.is_set()) else {}
    apple = apple_preflight(stop) if system and probes and not (stop and stop.is_set()) else {}
    brew = brew_inventory(stop) if probes and not (stop and stop.is_set()) else {}
    for category, data in [('Ferramentas', tools), ('Docker', docker), ('Homebrew: prévias', brew), ('Apps Apple: pré-condições', apple), ('Requisitos de projetos', requirements)]:
        if data:
            items.append(Finding(category, category, category, 'Resultado de consulta; revisar evidências.', evidence=json.dumps(data, ensure_ascii=False, indent=2)))
    return {'schema': 1, 'created': time.time(), 'home': str(policy.home),
            'free': shutil.disk_usage(policy.home).free, 'days': days,
            'findings': [asdict(f) for f in items],
            'tools': tools, 'docker': docker, 'apple': apple, 'requirements': requirements, 'brew': brew, 'apps': apps,
            'note': 'Inventário parcial; entradas aninhadas não devem ser somadas. Dados desconhecidos não são zero.'}


def make_plan(findings, policy):
    return {'schema': 1, 'home': str(policy.home), 'created': time.time(),
            'protected': sorted(map(str, policy.roots)),
            'projects': str(policy.projects), 'days': policy.days,
            'items': [dict(asdict(f), fingerprint=tree_fingerprint(Path(f.path))) for f in findings if f.action in ('delete-cache', 'delete-node-modules', 'delete-reviewed') and not policy.protected(Path(f.path))]}


def tree_fingerprint(path):
    digest = hashlib.sha256()
    if not path.is_dir():
        s = path.lstat()
        digest.update(repr((s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)).encode())
        return digest.hexdigest()
    def failed(exc):
        raise exc
    for folder, dirs, files in os.walk(path, followlinks=False, onerror=failed):
        PROGRESS('Validando plano: ' + str(folder))
        dirs.sort()
        for name in sorted(dirs + files):
            child = Path(folder) / name
            s = child.lstat()
            digest.update(repr((str(child.relative_to(path)), s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)).encode())
    return digest.hexdigest()


def execute_plan(plan, policy, apply=False, record=None):
    from maintenance import file_action
    if os.geteuid() == 0:
        raise ValueError('Executor de caches não aceita root.')
    if plan.get('schema') != 1 or plan.get('home') != str(policy.home):
        raise ValueError('Plano incompatível com usuário/configuração.')
    if plan.get('protected') != sorted(map(str, policy.roots)):
        raise ValueError('Proteções diferentes do plano; use a mesma configuração.')
    if plan.get('projects') != str(policy.projects) or plan.get('days') != policy.days:
        raise ValueError('Raiz de projetos ou retenção diferente do plano.')
    if apply and not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError('Python sem exclusão resistente a substituição por symlink.')
    allowed = {str(policy.home / p) for p in CACHES}
    checked = []
    seen = set()
    for item in plan['items']:
        p = Path(item['path'])
        if str(p) in seen:
            raise ValueError('Alvo duplicado no plano')
        seen.add(str(p))
        is_cache = str(p) in allowed and item.get('action') == 'delete-cache'
        is_reviewed = item.get('action') == 'delete-reviewed' and file_action(p, policy) is not None
        is_project = (item.get('action') == 'delete-node-modules' and p.name == 'node_modules'
                      and inside(p.resolve(), policy.projects) and p.parent != policy.projects)
        if is_project:
            current = project_findings(p.parent, policy, policy.days)
            is_project = any(f.path == str(p) and f.action == 'delete-node-modules' for f in current)
        if (not (is_cache or is_project or is_reviewed)
                or policy.protected(p) or policy.linked(p) or not (p.is_dir() or (is_reviewed and p.is_file()))
                or identity(p) != item.get('identity') or p.stat().st_uid != os.getuid()):
            raise ValueError(f'Alvo inválido ou alterado: {p}')
        if tree_fingerprint(p) != item.get('fingerprint'):
            raise ValueError(f'Conteúdo alterado desde o plano: {p}')
        # Proteções aninhadas e links internos impedem limpeza cega.
        def fail_closed(exc):
            raise exc
        for folder, dirs, files in (os.walk(p, followlinks=False, onerror=fail_closed) if p.is_dir() else []):
            for name in dirs + files:
                child = Path(folder) / name
                if policy.protected(child) or child.lstat().st_dev != p.stat().st_dev or (child.is_symlink() and is_cache):
                    raise ValueError(f'Conteúdo protegido/link/mount: {child}')
        checked.append(p)
    results = []
    for p in checked:
        if apply:
            before = shutil.disk_usage(policy.home).free
            try:
                item = next(i for i in plan['items'] if i['path'] == str(p))
                if item['action'] == 'delete-node-modules' and not any(f.path == str(p) and f.action == 'delete-node-modules' for f in project_findings(p.parent, policy, policy.days)):
                    raise ValueError('Projeto ativo ou condições de reinstalação alteradas')
                if item['action'] == 'delete-reviewed' and file_action(p, policy) is None:
                    raise ValueError('Condições de remoção alteradas')
                if identity(p) != item['identity'] or tree_fingerprint(p) != item['fingerprint']:
                    raise ValueError('Alvo mudou após validação')
                if record:
                    record({'path': str(p), 'status': 'iniciando', 'free_before': before})
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                results.append(f'removido: {p}')
                if record:
                    record({'path': str(p), 'status': 'removido', 'free_after': shutil.disk_usage(policy.home).free})
            except (OSError, ValueError) as exc:
                results.append(f'falha parcial possível: {p}: {exc}')
                if record:
                    record({'path': str(p), 'status': 'falha parcial possível', 'error': str(exc)})
                break
        else:
            results.append(f'simulação: {p}')
    return results


def compare_reports(before, after):
    old = {f['path']: f['size'] for f in before['findings']}
    return [{'path': f['path'], 'delta': f['size'] - old[f['path']]}
            for f in after['findings'] if f['size'] is not None and old.get(f['path']) is not None]


def save_new(path, value):
    # Não sobrescrever relatórios ou seguir symlink no destino.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
