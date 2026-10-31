"""Manutenção explícita do bundle local Cowork; nenhuma alteração no Claude Code."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from dev_audit import Policy, identity, tree_fingerprint


def assert_closed(vm):
    result = subprocess.run(['/bin/ps', '-axo', 'comm='], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError('Não foi possível verificar processos; operação bloqueada.')
    if any('/Claude.app/' in line or line.strip().endswith('/Claude') for line in result.stdout.splitlines()):
        raise ValueError('Feche o Claude Desktop completamente antes de aplicar.')
    if vm.exists():
        result = subprocess.run(['/usr/sbin/lsof', '-nP', '+D', str(vm)], capture_output=True, text=True, timeout=20)
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise ValueError('Não foi possível verificar arquivos abertos da VM.')
        if result.stdout.strip():
            raise ValueError('Há processos usando o bundle da VM; operação bloqueada.')


def paths(home):
    base = Path(home).resolve() / 'Library/Application Support/Claude'
    return base / 'claude_desktop_config.json', base / 'vm_bundles'


def read_config(config, home):
    if Policy(home).linked(config) or not config.is_file():
        raise ValueError('Configuração ausente ou caminho com link; abra o arquivo pelo Claude Desktop.')
    raw = config.read_bytes()
    def unique(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError('JSON com chaves duplicadas; corrija antes de aplicar.')
            data[key] = value
        return data
    data = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(data, dict) or not isinstance(data.get('preferences', {}), dict):
        raise ValueError('Estrutura de configuração não reconhecida.')
    return raw, data


def status(home):
    config, vm = paths(home)
    _, data = read_config(config, home)
    return {'secureVmFeaturesEnabled': data.get('preferences', {}).get('secureVmFeaturesEnabled', 'ausente'),
            'vm_bundles_exists': vm.exists() or vm.is_symlink(),
            'note': 'A chave no JSON local depende da versão; verificar comportamento após reabrir/atualizar o aplicativo.'}


def disable(home, apply=False, protected=()):
    config, vm = paths(home)
    raw, data = read_config(config, home)
    done = data.get('preferences', {}).get('secureVmFeaturesEnabled') is False
    if done and not vm.exists() and not vm.is_symlink():
        return {'state': 'já aplicado', 'backup': None}
    if not apply:
        return {'state': 'simulação', 'change_preference': not done, 'remove_vm_bundles': vm.exists(),
                'note': 'Alterará apenas a preferência indicada e removerá somente vm_bundles. Exclusão permanente, sem esvaziar a Lixeira.'}
    if os.geteuid() == 0:
        raise ValueError('Execute como seu usuário, sem sudo.')
    policy = Policy(home, protected)
    if policy.linked(vm) or policy.protected(vm) or policy.protected(config):
        raise ValueError('Caminho protegido ou link simbólico; operação bloqueada.')
    if config.stat().st_uid != os.getuid():
        raise ValueError('Configuração pertence a outro usuário.')
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError('Python sem exclusão resistente a troca por symlink.')
    stamp = identity(config)
    vm_stamp = identity(vm) if vm.exists() else None
    fingerprint = tree_fingerprint(vm) if vm.exists() else None
    if vm.exists():
        if not vm.is_dir() or vm.stat().st_uid != os.getuid():
            raise ValueError('Bundle não é diretório do usuário.')
        def fail(exc):
            raise exc
        for directory, dirs, files in os.walk(vm, followlinks=False, onerror=fail):
            for name in dirs + files:
                p = Path(directory) / name
                if p.is_symlink() or policy.protected(p) or p.lstat().st_dev != vm.stat().st_dev:
                    raise ValueError('Bundle contém links, mount ou artefatos protegidos.')
    assert_closed(vm)
    if identity(config) != stamp or config.read_bytes() != raw:
        raise ValueError('Configuração mudou durante a validação.')
    backup = config.with_name(config.name + f'.backup-{time.time_ns()}')
    fd = os.open(str(backup), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    data.setdefault('preferences', {})['secureVmFeaturesEnabled'] = False
    fd, temporary = tempfile.mkstemp(prefix='.cowork-config-', dir=config.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if identity(config) != stamp or config.read_bytes() != raw:
            raise ValueError('Configuração mudou; backup preservado.')
        os.replace(temporary, config)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if vm_stamp is not None:
        try:
            assert_closed(vm)
            if policy.linked(vm) or identity(vm) != vm_stamp or tree_fingerprint(vm) != fingerprint:
                raise ValueError('Bundle mudou durante a operação.')
            shutil.rmtree(vm)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f'Preferência salva; remoção incompleta ou bloqueada. Backup em {backup}. Motivo: {exc}') from exc
    return {'state': 'aplicado', 'backup': str(backup), 'vm_removed': vm_stamp is not None,
            'note': 'Reabra o app e verifique chats/MCP e se a VM permanece ausente. O backup contém apenas a configuração, não a VM.'}
