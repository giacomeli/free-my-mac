"""Interface fzf e auditoria cancelável em processo isolado."""
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from dev_audit import Finding, Policy, audit, make_plan, save_new
from free_my_mac import human, safe_text


def eligible_ids(items):
    return [i for i, f in enumerate(items) if f['action'] in ('delete-cache', 'delete-node-modules', 'delete-reviewed')]


def rows_for(items, ids=None):
    return '\n'.join(f"{i}\t{safe_text(items[i]['category'])} | {human(items[i]['size']) if items[i]['size'] is not None else 'não medido'} | {safe_text(items[i]['path'])}"
                     for i in (range(len(items)) if ids is None else ids))


def fzf_environment(env):
    return {k: v for k, v in env.items() if not k.startswith('FZF_')}


def choose(lines, title, preview=None, multi=False):
    argv = [shutil.which('fzf'), '--layout=reverse', '--border=rounded', '--height=90%',
            '--delimiter=\t', '--with-nth=2..', '--prompt=Buscar: ', '--border-label=FREE MY MAC',
            '--header=' + title, '--with-shell=/bin/sh -c']
    if multi:
        argv += ['--multi', '--bind=ctrl-a:select-all,ctrl-d:deselect-all',
                 '--footer=Tab: marcar | Ctrl-A: todos os filtrados | Ctrl-D: desmarcar | Enter: aceitar | Esc: voltar']
    else:
        argv += ['--footer=Digite para filtrar | Enter: abrir/voltar | Esc: voltar']
    if preview:
        argv += ['--preview=' + preview, '--preview-window=right:55%:wrap']
    result = subprocess.run(argv, input=lines + '\n', text=True, stdout=subprocess.PIPE, env=fzf_environment(os.environ))
    if result.returncode in (1, 130):
        return []
    if result.returncode != 0:
        raise RuntimeError(f'fzf terminou com código {result.returncode}')
    return [int(line.split('\t', 1)[0]) for line in result.stdout.splitlines() if line]


def worker(config, destination):
    import dev_audit
    def progress(message):
        print(json.dumps({'message': message}), flush=True)
    dev_audit.PROGRESS = progress
    policy = Policy(config['home'], config['protected'], config['projects'], config['days'])
    if config.get('operation') == 'plan':
        progress('Validando metadados dos itens selecionados para o plano')
        result = make_plan([Finding(**f) for f in config['items']], policy)
    else:
        result = audit(policy, Path(config['projects']), config['days'], config['system'], config['measure'], probes=config['probes'])
    save_new(destination, result)
    progress('Concluído')


def background(config, directory, operation='audit'):
    config_path = directory / f'config-{time.time_ns()}.json'
    output_path = directory / f'result-{time.time_ns()}.json'
    save_new(config_path, dict(config, operation=operation))
    started = time.monotonic()
    stage_started = started
    current = 'Iniciando auditoria' if operation == 'audit' else 'Preparando plano'
    buffer = b''
    with (directory / 'worker-error.log').open('w+') as errors:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'worker', str(config_path), str(output_path)],
                                   stdout=subprocess.PIPE, stderr=errors, start_new_session=True)
        try:
            while True:
                if select.select([process.stdout], [], [], .15)[0]:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        message = json.loads(line)['message']
                        if message != current:
                            stage_started = time.monotonic()
                        current = message
                elapsed = time.monotonic() - started
                stage_elapsed = time.monotonic() - stage_started
                spinner = "|/-\\"[int(elapsed * 5) % 4]
                text = f"{spinner} {elapsed:.0f}s total | {stage_elapsed:.0f}s nesta etapa | {safe_text(current)}"
                width = shutil.get_terminal_size().columns
                print('\r\033[K' + text[:max(20, width - 1)], end='', flush=True)
                if process.poll() is not None:
                    break
            print('\r\033[K', end='')
            if process.returncode:
                errors.seek(0)
                raise RuntimeError('Auditoria falhou: ' + safe_text(errors.read()[-3000:]))
            result = json.loads(output_path.read_text())
            print(f"{'LEITURA CONCLUÍDA' if operation == 'audit' else 'PLANO VALIDADO'} em {time.monotonic() - started:.1f}s. Nenhuma limpeza executada.", flush=True)
            return result
        finally:
            # O grupo pertence exclusivamente ao worker e às consultas iniciadas por ele.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.stdout.close()


def launch(policy, projects, days, system, measure, probes):
    if not shutil.which('fzf'):
        raise ValueError('fzf não encontrado. Use --report ou --ui curses.')
    if os.geteuid() == 0:
        raise ValueError('Abra a interface sem sudo para auditar seu usuário.')
    config = {'home': str(policy.home), 'protected': list(map(str, policy.roots)),
              'projects': str(Path(projects).expanduser().resolve()), 'days': days,
              'system': system, 'measure': measure, 'probes': probes}
    print('FREE MY MAC | fzf | Ctrl-C cancela a auditoria e suas consultas.', flush=True)
    with tempfile.TemporaryDirectory(prefix='free-my-mac-ui-') as temporary:
        directory = Path(temporary)
        try:
            report = background(config, directory)
            items = report['findings']
            report_path = directory / 'report.json'
            save_new(report_path, report)
            preview = shlex.join([sys.executable, str(Path(__file__).resolve()), 'detail', str(report_path)]) + ' {1}'
            selected = set()
            notice = 'Leitura concluída. Remoções exigem revisão e confirmação explícita.'
            while True:
                menu = ['0\tExplorar todos os achados (somente leitura)',
                        '1\tSelecionar ações elegíveis por categoria',
                        '2\tRevisar seleção atual', '3\tSalvar plano selecionado',
                        '4\tSalvar relatório completo', '5\tDesmarcar tudo', '6\tSair',
                        '7\tManutenção: Docker, Homebrew, gestores, Apple Intelligence, Cowork',
                        '8\tExecutar seleção atual (revisão e confirmação)',
                        '9\tRemoção individual avançada: resíduos, instalações e apps Apple']
                choice = choose('\n'.join(menu), f'{len(items)} achados | {len(eligible_ids(items))} elegíveis | {len(selected)} selecionados\n{notice}')
                if not choice or choice[0] == 6:
                    return
                action = choice[0]
                if action == 0:
                    choose(rows_for(items), 'INVENTÁRIO | Enter/Esc volta ao menu. Prévia à direita.', preview)
                elif action == 1:
                    eligible = eligible_ids(items)
                    categories = ['Todas'] + sorted({items[i]['category'] for i in eligible})
                    cat = choose('\n'.join(f'{i}\t{name}' for i, name in enumerate(categories)), 'Escolha a categoria de ações disponíveis')
                    if cat:
                        subset = [i for i in eligible if cat[0] == 0 or items[i]['category'] == categories[cat[0]]]
                        if subset:
                            picked = choose(rows_for(items, subset), 'Marque ações; Enter adiciona à seleção. Nada será apagado.', preview, True)
                            selected.update(i for i in picked if i in subset)
                        else:
                            notice = 'Nenhuma ação executável nesta categoria.'
                elif action == 2:
                    if selected:
                        choose(rows_for(items, sorted(selected)), 'SELEÇÃO ATUAL | somente revisão; nenhuma execução', preview)
                    else:
                        notice = 'Nenhum item selecionado.'
                elif action in (3, 8):
                    if not selected:
                        notice = 'Selecione ao menos uma ação antes de salvar.'
                        continue
                    plan = background(dict(config, items=[items[i] for i in sorted(selected)]), directory, 'plan')
                    destination = Path.cwd() / f'free-my-mac-plan-{time.time_ns()}.json'
                    save_new(destination, plan)
                    notice = f'Plano salvo: {destination}. Não executado.'
                    if action == 8:
                        from maintenance import live_command
                        argv = [sys.executable, str(Path(__file__).with_name('free_my_mac.py')), '--plan', str(destination), '--apply', '--projects', str(policy.projects), '--days', str(policy.days)]
                        for root in policy.roots:
                            argv += ['--protect', str(root)]
                        code = live_command(argv)
                        selected.clear()
                        notice = f'Executor terminou com código {code}. Reinicie a auditoria antes de selecionar novamente.'
                elif action == 4:
                    destination = Path.cwd() / f'free-my-mac-report-{time.time_ns()}.json'
                    save_new(destination, report)
                    notice = f'Relatório salvo: {destination}'
                elif action == 5:
                    selected.clear()
                    notice = 'Seleção limpa; arquivos preservados.'
                elif action == 7:
                    from maintenance import menu
                    menu(policy.roots, policy)
                    notice = 'Manutenção encerrada. Reinicie a auditoria para atualizar os achados.'
                elif action == 9:
                    from explicit_removal import allowed, remove
                    ids = [i for i, f in enumerate(items) if allowed(Path(f['path']), policy)]
                    picked = choose(rows_for(items, ids), 'AVANÇADO: selecione UM alvo; dados/configurações podem ser perdidos', preview) if ids else []
                    if picked:
                        try:
                            remove(items[picked[0]]['path'], policy, True)
                        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                            print('Falha: ' + str(exc))
                        input('Enter para voltar...')
        except KeyboardInterrupt:
            print('\nInterrompido. Se uma execução já começou, pode haver alterações parciais; confira os registros free-my-mac-*.jsonl/json.', flush=True)


if __name__ == '__main__':
    if sys.argv[1] == 'worker':
        worker(json.loads(Path(sys.argv[2]).read_text()), Path(sys.argv[3]))
    elif sys.argv[1] == 'detail':
        f = json.loads(Path(sys.argv[2]).read_text())['findings'][int(sys.argv[3])]
        for key in ('category', 'path', 'owner', 'action', 'reason', 'evidence'):
            print(key.upper() + ':')
            for line in str(f.get(key, '')).splitlines():
                print(safe_text(line))
            print()
