"""Interface de revisão de achados; execução fica em comando separado."""
import concurrent.futures
import curses
from pathlib import Path
import threading
import time
import textwrap

from dev_audit import Finding, audit, make_plan, save_new
from free_my_mac import human, put


def display(screen, policy, projects, days, system, measure, probes):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(100)
    stop = threading.Event()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(audit, policy, projects, days, system, measure, stop, probes)
    report, rows, selected, chosen, detail, category, scroll = None, [], 0, set(), False, 0, 0
    last_render = None
    message = 'Auditoria em andamento. q cancela; comando atual pode levar até 30 s.'
    try:
        while True:
            if future is not None and future.done():
                try:
                    report = future.result()
                    message = 'Nenhuma limpeza executada. Selecione somente as ações que deseja incluir no plano.'
                except Exception as exc:
                    message = f'Auditoria falhou: {exc}'
                future = None
            categories = ['Todas'] + sorted({f['category'] for f in report['findings']}) if report else ['Todas']
            if report:
                rows = [f for f in report['findings'] if category == 0 or f['category'] == categories[category]]
            selected = min(selected, max(0, len(rows) - 1))
            render_state = (report is not None, future is not None, category, selected, frozenset(chosen), detail, scroll, message, screen.getmaxyx())
            if render_state != last_render:
                screen.erase()
                height, width = screen.getmaxyx()
                status = 'Leitura concluída' if report is not None else ('Auditoria em andamento' if future is not None else 'Falha na auditoria')
                put(screen, 0, f'FREE MY MAC | {status} | nenhuma limpeza nesta tela', curses.A_BOLD)
                local_count = sum(f['path'] in chosen for f in rows)
                eligible = sum(f['action'] in ('delete-cache', 'delete-node-modules', 'delete-reviewed') for f in rows)
                put(screen, 1, f'{categories[category]} | {len(rows)} achados | {eligible} elegíveis | {local_count} nesta categoria / {len(chosen)} no total')
                put(screen, 2, 'Setas: navegar | Enter: detalhes | Tab: categoria | q: sair')
                put(screen, 3, 'Espaço: marcar | a: categoria | A: todos elegíveis | c: desmarcar tudo | p: plano | s: relatório')
                put(screen, 4, message)
                if rows and detail:
                    f = rows[selected]
                    lines = [f['path'], f"{f['category']} / {f['owner']}",
                             f"Tamanho: {human(f['size'])}; ação: {f['action']}",
                             f['reason'], f['evidence'], 'Execução de caches exige fechar os gerenciadores e confirmação em comando separado.']
                    wrapped = [part for line in lines for part in textwrap.wrap(line, max(10, width - 2))]
                    scroll = min(scroll, max(0, len(wrapped) - max(1, height - 6)))
                    for i, line in enumerate(wrapped[scroll:]):
                        put(screen, i + 5, line)
                elif rows:
                    available = max(1, height - 6)
                    offset = max(0, selected - available + 1)
                    for i, f in enumerate(rows[offset:offset + available], offset):
                        eligible = f['action'] in ('delete-cache', 'delete-node-modules', 'delete-reviewed')
                        mark = ('[x]' if f['path'] in chosen else '[ ]') if eligible else ' - '
                        label = f['action'] if eligible else ('protegido' if f['action'] == 'protected' else 'somente análise')
                        size_label = human(f['size']) if f['size'] is not None or measure else 'não medido'
                        put(screen, 5 + i - offset,
                            f"{mark} {size_label:>12} [{label}] {f['category']}: {f['path']}",
                            curses.A_REVERSE if i == selected else 0)
                screen.refresh()
                last_render = render_state
            key = screen.getch()
            if key in (ord('q'), 27):
                break
            if key == 9:
                category = (category + 1) % len(categories)
                selected, detail = 0, False
            elif key == curses.KEY_DOWN:
                if detail:
                    scroll += 1
                else:
                    selected = min(selected + 1, max(0, len(rows) - 1))
            elif key == curses.KEY_UP:
                if detail:
                    scroll = max(0, scroll - 1)
                else:
                    selected = max(0, selected - 1)
            elif key in (10, 13):
                detail = not detail
                scroll = 0
            elif key in (ord('a'), ord('A')) and report:
                source = rows if key == ord('a') else report['findings']
                chosen.update(f['path'] for f in source if f['action'] in ('delete-cache', 'delete-node-modules', 'delete-reviewed'))
                message = 'Selecionados apenas itens elegíveis; nenhuma limpeza executada.'
            elif key == ord('c'):
                chosen.clear()
                message = 'Seleção limpa; nenhum arquivo alterado.'
            elif key == 32 and rows:
                f = rows[selected]
                if f['action'] in ('delete-cache', 'delete-node-modules', 'delete-reviewed'):
                    chosen.symmetric_difference_update({f['path']})
                else:
                    message = 'Achado apenas para revisão ou protegido; execução não disponível.'
            elif key in (ord('p'), ord('s')) and report:
                if key == ord('p') and not chosen:
                    message = 'Nenhuma ação selecionada para salvar.'
                    continue
                destination = Path.cwd() / f"free-my-mac-{'plan' if key == ord('p') else 'report'}-{time.time_ns()}.json"
                try:
                    data = report if key == ord('s') else make_plan(
                        [Finding(**f) for f in report['findings'] if f['path'] in chosen], policy)
                    save_new(destination, data)
                    message = f'Salvo: {destination.name}'
                except (OSError, ValueError) as exc:
                    message = f'Falha ao salvar: {exc}'
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
