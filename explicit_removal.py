"""Remoção individual de resíduos e instalações fora de gerenciadores.

Não remonta volumes, não altera SIP e não cria snapshots de boot.
"""
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import time

from dev_audit import Policy, identity, inside, tree_fingerprint


APPLE_APPS = {'Maps.app', 'Music.app', 'TV.app', 'Stocks.app', 'News.app',
              'Books.app', 'Podcasts.app', 'Home.app', 'Freeform.app', 'Chess.app',
              'GarageBand.app', 'iMovie.app', 'Keynote.app', 'Numbers.app', 'Pages.app'}


def allowed(path, policy):
    p = Path(path)
    if not p.is_absolute() or '..' in p.parts or p == policy.home or policy.protected(p):
        return False
    if any(a.is_symlink() for a in [p, *p.parents]):
        return False
    if p.parent in (Path('/Applications'), Path('/System/Applications')) and p.name in APPLE_APPS:
        return True
    if p.parent == Path('/Library/Fonts') and p.suffix.lower() in {'.otf', '.ttf', '.ttc', '.dfont'}:
        return True
    if inside(p, Path('/Library/Logs')) and p.is_file() and p.suffix in {'.gz', '.old', '.log'}:
        return time.time() - p.stat().st_mtime >= policy.days * 86400
    # Versão específica, nunca a árvore inteira /opt, /usr/local ou um gestor.
    if re.fullmatch(r'/usr/local/share/dotnet/(sdk|shared/[^/]+)/[0-9][^/]*', str(p)):
        return True
    if re.fullmatch(r'/Library/Frameworks/Python.framework/Versions/[0-9]+\.[0-9]+', str(p)):
        return True
    if p in {Path('/opt') / name for name in ('miniforge3', 'miniconda3', 'anaconda3')}:
        return True
    h = policy.home
    if p in {h / name for name in ('miniforge3', 'miniconda3', 'anaconda3')}:
        return True
    if p.parent in (h / 'Library/Application Support', h / 'Library/Caches', h / 'Library/Preferences'):
        # Credenciais, conversas e conteúdo de agentes jamais entram como "resíduo" genérico.
        reserved = ('claude', 'codex', 'cursor', 'openai', 'anthropic', 'huggingface', 'ollama')
        return not any(name in p.name.casefold() for name in reserved)
    if p.parent == h / 'Library/Application Support/Herd/bin':
        return p.name.startswith('php')
    return False


def validate(path, policy):
    p = Path(path)
    if not allowed(p, policy) or not p.exists():
        raise ValueError('Caminho fora do catálogo explícito, protegido, link ou inexistente.')
    device = p.lstat().st_dev
    if os.path.ismount(p):
        raise ValueError('Não removemos pontos de montagem.')
    def failed(exc):
        raise exc
    if p.is_dir():
        for folder, dirs, files in os.walk(p, followlinks=False, onerror=failed):
            for name in dirs + files:
                child = Path(folder) / name
                if policy.protected(child) or child.lstat().st_dev != device or os.path.ismount(child):
                    raise ValueError('Alvo contém dados protegidos ou outro volume: ' + str(child))
    elif not p.is_file():
        raise ValueError('Tipo de arquivo não suportado.')
    if os.statvfs(p).f_flag & getattr(os, 'ST_RDONLY', 1):
        raise ValueError('Volume somente leitura. Desligar SIP não torna o volume selado gravável. O script não remonta nem modifica snapshots de boot.')
    return dict(identity=identity(p), fingerprint=tree_fingerprint(p))


def remove(path, policy, apply=False, expected=None):
    p = Path(path).expanduser().absolute()
    before = validate(p, policy)
    print('REMOÇÃO INDIVIDUAL: ' + str(p))
    print('Desuso não foi comprovado. Dados/configurações locais serão perdidos; dependências podem deixar de funcionar.')
    if expected and before != expected:
        raise ValueError('Alvo mudou após a revisão.')
    if not apply:
        print('SIMULAÇÃO: nenhuma alteração.')
        return
    if not sys.stdin.isatty():
        raise ValueError('Remoção exige terminal interativo.')
    if input(f'Digite REMOVER {p.name} para confirmar: ') != f'REMOVER {p.name}':
        return
    # Falha fechada: processos com arquivos abertos ou consulta inconclusiva bloqueiam.
    lsof = shutil.which('lsof') or '/usr/sbin/lsof'
    args = [lsof, '-t', '+D', str(p)] if p.is_dir() else [lsof, '-t', '--', str(p)]
    answer = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if answer.stdout.strip() or answer.returncode not in (0, 1) or answer.stderr.strip():
        raise ValueError('Alvo em uso ou inspeção lsof inconclusiva; feche os aplicativos.')
    if validate(p, policy) != before:
        raise ValueError('Alvo mudou; repita a revisão.')
    if not os.access(p.parent, os.W_OK) and os.geteuid() != 0:
        from maintenance import live_command
        argv = ['/usr/bin/sudo', sys.executable, str(Path(__file__).resolve()), str(p), json.dumps(before)]
        for root in policy.roots:
            argv += [str(root)]
        if live_command(argv):
            raise ValueError('Remoção privilegiada falhou; confira a saída.')
        return
    if p.is_dir() and not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError('Python não suporta remoção resistente a troca por symlink.')
    log_root = Path('/var/log') if os.geteuid() == 0 else Path.cwd()
    log = log_root / f'free-my-mac-remove-{time.time_ns()}.jsonl'
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        def record(state):
            stream.write(json.dumps(dict(path=str(p), status=state, free=shutil.disk_usage(p.parent).free)) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        record('iniciando')
        try:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        except BaseException:
            record('falha; remoção parcial possível')
            raise
        record('removido')
    print('Registro: ' + str(log))


if __name__ == '__main__':
    if os.geteuid() != 0 or not os.environ.get('SUDO_UID'):
        sys.exit('Helper reservado à elevação solicitada pelo executor.')
    user = pwd.getpwuid(int(os.environ['SUDO_UID']))
    policy = Policy(Path(user.pw_dir), sys.argv[3:])
    try:
        remove(sys.argv[1], policy, True, json.loads(sys.argv[2]))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        sys.exit(str(exc))
