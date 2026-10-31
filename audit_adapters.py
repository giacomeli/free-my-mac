"""Evidências e recomendações específicas; não executa manutenção."""
import json
from pathlib import Path
import shutil
import time

from dev_audit import Finding, run


def read_plist(path):
    result = run(['/usr/bin/plutil', '-convert', 'json', '-o', '-', str(path)], timeout=2)
    if not result['ok']:
        raise ValueError('plist indisponível')
    data = json.loads(result['output'])
    if not isinstance(data, dict):
        raise ValueError('plist não é dicionário')
    return data


def app_inventory(home, stop=None):
    result = {}
    for root in (Path('/Applications'), home / 'Applications', Path('/System/Applications')):
        for app in root.glob('*.app'):
            if stop and stop.is_set():
                return result
            try:
                info = read_plist(app / 'Contents/Info.plist')
                result[info['CFBundleIdentifier']] = {'path': str(app), 'version': info.get('CFBundleShortVersionString', '?')}
            except (OSError, ValueError, KeyError):
                continue
    return result


def enrich_findings(items, policy, days, requirements, stop=None):
    apps = app_inventory(policy.home, stop)
    referenced = '\n'.join(requirements.values())
    instructions = {
        'Node': 'Conferir .nvmrc/.node-version e engines em package.json. Desinstalar pela ferramenta proprietária; preservar a versão ativa e pacotes globais necessários.',
        'Python': 'Conferir .python-version, pyproject.toml, ambientes e shebangs. Conda/Miniforge contém ambientes e cache de pacotes distintos; nunca excluir a distribuição inteira como cache.',
        'Versões': 'Conferir global.json, .tool-versions, mise.toml e toolchains. Uma referência fora da raiz pesquisada pode exigir esta versão.',
        'Dependências': 'Store/ambiente compartilhado: usar manutenção nativa. Remover cache pode afetar uso offline; não confundir com instalação do runtime.',
        'Apple SDK': 'DerivedData é reconstruível; Archives/dSYM podem ser necessários para distribuir e simbolicar crashes. Dispositivos/runtimes devem ser removidos pelo Xcode/Simulator, sem apagar volumes montados.',
        'Android': 'AVDs contêm dados e snapshots. Selecionar AVD no Device Manager e versões no SDK Manager; não confundir runtime compartilhado com um dispositivo.',
        'Cowork': 'Fechar Claude e identificar versão/opção oficial de armazenamento. Bundle pode conter estado local; não apagar sessões nem assumir reconstrução automática.',
        'Herd': 'Inspecionar bin/php, configurações, certificados e serviços; preservar bancos e sites. Usar desinstalação oficial para serviços.',
        'Fontes': 'Selecionar explicitamente famílias a manter no Catálogo de Fontes. Para fonte instalada por cask, preferir brew uninstall --cask NOME após verificar o cask.',
        'Logs': 'Aplicar idade apenas a arquivos rotacionados, não ativos. Logs de sistema e unified logging exigem retenção nativa; tamanho não indica se o processo ainda escreve.',
        'Instaladores': 'Revisar se a imagem está montada e se o instalador pode ser baixado novamente antes de remover.',
        'Apps e resíduos': 'Ausência de app não prova resíduo: nomes não correspondem necessariamente ao bundle ID e dados podem ser compartilhados.',
        'Atualizações e caches': 'Identificar app/updater e confirmar que não há atualização em andamento. Não aplicar limpeza genérica a downloads do sistema.',
        'Instalações': 'Determinar gerenciador pelo prefixo e recibos; coexistência ARM/Intel pode ser intencional. Nunca remover /opt ou /usr/local inteiros.',
        'Serviços': 'Verificar Program/ProgramArguments, origem e processo antes de desativar. Não executar launchctl unload automaticamente.',
        'Apps Apple': 'Módulo avançado: examinar bundle e pré-condições. Sistema selado somente leitura bloqueia remoção; não modificar proteções automaticamente.',
    }
    for f in items:
        if stop and stop.is_set():
            break
        p = Path(f.path)
        f.evidence += '\n' + instructions.get(f.category, '')
        if f.category in ('Node', 'Python', 'Versões'):
            f.evidence += '\nVersão/diretório: ' + p.name
            if p.name.lstrip('v') in referenced and len(p.name) > 2:
                f.reason = 'Possível versão referenciada por projeto: preservar até validar correspondência.'
        if f.category in ('Logs', 'Instaladores'):
            try:
                age = (time.time() - p.stat().st_mtime) / 86400
                f.reason = f'Última modificação há {age:.0f} dias; retenção proposta {days} dias. ' + ('Fora da janela; revisar atividade.' if age > days else 'Dentro da janela; manter.')
            except OSError:
                pass
        if f.category == 'Apps e resíduos' and p.name in apps:
            f.reason = 'Bundle ID corresponde a app instalado; não classificar como resíduo.'
            f.evidence += '\n' + json.dumps(apps[p.name], ensure_ascii=False)
        if f.category == 'Serviços' or p.suffix == '.plist':
            try:
                info = read_plist(p)
                executable = info.get('Program') or (info.get('ProgramArguments') or [''])[0]
                f.evidence += f'\nExecutável: {executable}; existe: {Path(executable).exists() if executable else "desconhecido"}'
            except (OSError, ValueError):
                pass
        if p.suffix == '.app':
            try:
                info = read_plist(p / 'Contents/Info.plist')
                f.evidence += f'\nBundle ID: {info.get("CFBundleIdentifier")}; versão: {info.get("CFBundleShortVersionString")}'
            except (OSError, ValueError):
                pass
    return apps


def brew_inventory(stop=None):
    brew = shutil.which('brew')
    if not brew:
        return {}
    result = {}
    for name, args in [('installed', ['info', '--json=v2', '--installed']),
                       ('cleanup_preview', ['cleanup', '--dry-run']),
                       ('autoremove_preview', ['autoremove', '--dry-run'])]:
        if stop and stop.is_set():
            break
        result[name] = run([brew, *args])
    result['note'] = 'Prévia do Homebrew, não histórico de uso. Desinstalação pode rodar scripts de casks; revisão individual necessária.'
    return result
