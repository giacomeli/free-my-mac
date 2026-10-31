#!/usr/bin/env python3
"""Auditoria e manutenção seletiva do macOS, com interface fzf."""
import argparse
import concurrent.futures
import curses
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading


def human(size):
    if size is None:
        return "indisponível"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024


def measure(path):
    """du não segue links; limite por entrada evita bloqueio indefinido."""
    if path.is_symlink():
        return None, "link; não percorrido"
    try:
        if path.is_file():
            return path.stat().st_blocks * 512, 'estimativa'
    except OSError:
        return None, 'acesso indisponível'
    try:
        result = subprocess.run(
            ["/usr/bin/du", "-skx", str(path)], capture_output=True,
            text=True, timeout=30,
        )
        if result.returncode:
            return None, "incompleto: acesso negado ou arquivo alterado"
        return int(result.stdout.split()[0]) * 1024, "estimativa"
    except subprocess.TimeoutExpired:
        return None, "tempo excedido (30 s)"
    except (OSError, ValueError, IndexError):
        return None, "não foi possível medir"


def scan(path, stop=None):
    rows = []
    for child in path.iterdir():
        if stop is not None and stop.is_set():
            break
        size, status = measure(child)
        if "sketchpad" in child.name.casefold():
            status += "; sketchpad: preservar"
        rows.append((child, size, status))
    return sorted(rows, key=lambda row: (row[1] is not None, row[1] or 0), reverse=True)


def safe_text(value):
    return "".join(c if c.isprintable() else "?" for c in str(value))


def put(screen, y, text, style=0):
    height, width = screen.getmaxyx()
    if y < height - 1 and width > 1:
        try:
            screen.addnstr(y, 0, safe_text(text), width - 1, style)
        except curses.error:
            pass


GUIDE = [
    "1. Veja o espaço disponível no cabeçalho.",
    "2. Aguarde a medição; pastas grandes podem levar tempo.",
    "3. Use setas e Enter para explorar uma pasta grande.",
    "4. Preserve sketchpads, sessões, modelos e artefatos dos agentes.",
    "5. Investigue caches na ferramenta que os criou antes de decidir.",
    "6. Faça inventário de apps de início nos Ajustes do Sistema.",
    "7. Compare CPU e pressão de memória no Monitor de Atividade",
    "   durante seu trabalho habitual, antes de propor otimizações.",
    "8. Repita o relatório em outro dia para investigar crescimento.",
    "",
    "Nenhum comando de exclusão, debloat ou ajuste é executado.",
    "APFS: clones, snapshots e espaço compartilhado impedem somas exatas.",
    "Medir tamanho não determina quanto uma exclusão liberaria.",
    "Erros e limites de tempo aparecem como indisponível, nunca zero.",
    "Links simbólicos não são seguidos; du permanece no mesmo filesystem.",
    "A leitura de arquivos locais pode gerar atividade de disco.",
    "",
    "Pressione g para voltar ao diagnóstico.",
]


def tui(screen, root):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(100)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    stop = threading.Event()
    current, selected, rows, guide = root, 0, [], False
    future = pool.submit(scan, current, stop)
    error = ""
    try:
        while True:
            if future is not None and future.done():
                try:
                    rows = future.result()
                except OSError as exc:
                    rows, error = [], f"Acesso indisponível: {exc}"
                future = None
            screen.erase()
            usage = shutil.disk_usage(current)
            put(screen, 0, "FREE MY MAC | Somente leitura | Todos os arquivos preservados", curses.A_BOLD)
            put(screen, 1, f"Livre: {human(usage.free)} / {human(usage.total)} | {current}")
            put(screen, 2, "Setas: selecionar | Enter: abrir | Backspace: subir | r: medir | g: guia | q: sair")
            height, _ = screen.getmaxyx()
            if guide:
                for i, line in enumerate(GUIDE):
                    put(screen, i + 4, line)
            elif future is not None:
                put(screen, 4, "Medindo... interface disponível; cada entrada tem limite de 30 segundos.")
                put(screen, 5, "q cancela após a entrada atual (até 30 s); demais entradas não serão medidas.")
            else:
                put(screen, 3, error or f"{len(rows)} entradas | tamanho alocado estimado; não é espaço recuperável")
                visible = max(1, height - 6)
                offset = max(0, selected - visible + 1)
                for i, (path, size, status) in enumerate(rows[offset:offset + visible], offset):
                    put(screen, 4 + i - offset, f"{human(size):>13}  {path.name}  [{status}]",
                        curses.A_REVERSE if i == selected else 0)
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), 27):
                break
            if key == ord("g"):
                guide = not guide
            if future is not None or guide:
                continue
            target = None
            if key == curses.KEY_DOWN:
                selected = min(max(0, len(rows) - 1), selected + 1)
            elif key == curses.KEY_UP:
                selected = max(0, selected - 1)
            elif key in (10, 13, curses.KEY_ENTER) and rows:
                candidate = rows[selected][0]
                if candidate.is_dir() and not candidate.is_symlink():
                    target = candidate
            elif key in (127, 8, curses.KEY_BACKSPACE):
                target = current.parent
            elif key == ord("r"):
                target = current
            if target is not None:
                current, selected, error = target, 0, ""
                future = pool.submit(scan, current, stop)
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, help="Pasta inicial; exige --browse")
    parser.add_argument("--report", action="store_true", help="Relatório textual em stdout")
    parser.add_argument('--browse', action='store_true', help='Abrir navegador de tamanhos legado')
    parser.add_argument('--ui', choices=('fzf', 'curses'), default='fzf', help='Interface interativa (padrão: fzf)')
    parser.add_argument('--projects', type=Path, default=Path.home() / 'Projects')
    parser.add_argument('--days', type=int, default=60, help='Janela de atividade dos projetos')
    parser.add_argument('--protect', type=Path, action='append', default=[], help='Raiz adicional preservada; repetível')
    parser.add_argument('--system', action='store_true', help='Incluir /opt, /usr/local e inventário de sistema')
    parser.add_argument('--measure', action='store_true', help='Medir tamanhos (pode levar vários minutos)')
    parser.add_argument('--no-probes', action='store_true', help='Não consultar comandos dos gerenciadores')
    parser.add_argument('--json', action='store_true', help='Emitir auditoria JSON em stdout')
    parser.add_argument('--save', type=Path, help='Salvar relatório novo; não sobrescreve')
    parser.add_argument('--compare', nargs=2, type=Path, metavar=('ANTES', 'DEPOIS'))
    parser.add_argument('--plan', type=Path, help='Validar/simular plano de arquivos salvo')
    parser.add_argument('--apply', action='store_true', help='Executar plano após confirmação no terminal')
    parser.add_argument('--cowork', choices=('status', 'disable'), help='Verificar ou preparar desativação da VM local Cowork')
    parser.add_argument('--maintenance', nargs='?', const='menu', help='Menu de ações nativas; list lista ações; ID simula, --apply executa')
    parser.add_argument('--remove-path', type=Path, help='Remoção individual de resíduo/instalação/app Apple do catálogo; simula sem --apply')
    args = parser.parse_args()
    if args.days < 1:
        parser.error('--days precisa ser positivo')
    if args.apply and not args.plan and args.cowork != 'disable' and not args.maintenance and not args.remove_path:
        parser.error('--apply exige --plan, --maintenance, --remove-path ou --cowork disable')
    if args.cowork and (args.plan or args.compare or args.browse):
        parser.error('--cowork não pode ser combinado com --plan, --compare ou --browse')
    if args.path and not args.browse:
        parser.error('--path exige --browse; na auditoria use --projects')
    root = (args.path or Path.home()).expanduser().resolve()
    if not root.is_dir():
        parser.error("--path precisa ser uma pasta existente")
    try:
        from dev_audit import Policy, audit, compare_reports, execute_plan, save_new
        import json
        policy = Policy(Path.home(), args.protect, args.projects, args.days)
        if args.remove_path:
            if args.plan or args.cowork or args.maintenance or args.browse or args.compare:
                parser.error('--remove-path exige modo exclusivo')
            from explicit_removal import remove
            remove(args.remove_path, policy, args.apply)
        elif args.maintenance:
            if args.plan or args.cowork or args.browse or args.compare:
                parser.error('--maintenance não pode ser combinado com outro modo')
            from maintenance import menu, perform, command_catalog
            if args.maintenance == 'menu':
                if not sys.stdin.isatty():
                    parser.error('Menu exige terminal interativo')
                menu(policy.roots, policy)
            elif args.maintenance == 'versions':
                from managed_cleanup import menu as managed_menu
                managed_menu(policy)
            elif args.maintenance == 'list':
                print(json.dumps(command_catalog(), ensure_ascii=False, indent=2))
            else:
                return perform(args.maintenance, args.apply, policy.roots)
        elif args.cowork:
            from cowork_maintenance import status, disable
            if args.cowork == 'status':
                print(json.dumps(status(Path.home()), ensure_ascii=False, indent=2))
            else:
                preview = disable(Path.home(), protected=args.protect)
                print(json.dumps(preview, ensure_ascii=False, indent=2))
                if args.apply and preview['state'] != 'já aplicado':
                    if not sys.stdin.isatty():
                        parser.error('--apply exige terminal interativo')
                    print('Feche o Claude Desktop. A remoção de vm_bundles será permanente; sessões externas a essa pasta serão preservadas.')
                    if input('Digite DESATIVAR COWORK para aplicar: ') == 'DESATIVAR COWORK':
                        print(json.dumps(disable(Path.home(), True, args.protect), ensure_ascii=False, indent=2))
                    else:
                        print('Cancelado.')
        elif args.compare:
            print(json.dumps(compare_reports(*[json.loads(p.read_text()) for p in args.compare]), indent=2))
        elif args.plan:
            import dev_audit
            import time
            last_progress = [0.0]
            def progress(message):
                if time.monotonic() - last_progress[0] >= 1:
                    print(safe_text(message), flush=True)
                    last_progress[0] = time.monotonic()
            dev_audit.PROGRESS = progress
            plan = json.loads(args.plan.read_text())
            for line in execute_plan(plan, policy, False):
                print(safe_text(line))
            if args.apply:
                if not sys.stdin.isatty():
                    parser.error('--apply exige confirmação em terminal interativo')
                print('Feche gerenciadores e projetos. A exclusão é permanente; reinstalação pode exigir downloads. Alterações manuais em node_modules serão perdidas.')
                if input('Digite APAGAR ITENS para executar: ') != 'APAGAR ITENS':
                    print('Cancelado.')
                    return 0
                import time
                journal = Path.cwd() / f'free-my-mac-execution-{time.time_ns()}.jsonl'
                descriptor = os.open(str(journal), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as stream:
                    def record(event):
                        stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                        stream.flush()
                        os.fsync(stream.fileno())
                    results = execute_plan(plan, policy, True, record)
                for line in results:
                    print(safe_text(line))
                print(f'Registro: {journal}')
                if any(line.startswith('falha') for line in results):
                    return 1
        elif not args.browse:
            if args.report or args.json or args.save:
                result = audit(policy, args.projects.expanduser(), args.days, args.system, args.measure, probes=not args.no_probes)
                if args.save:
                    save_new(args.save, result)
                if args.json:
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    print('AUDITORIA DEV | somente leitura | tamanhos aninhados não devem ser somados')
                    for f in result['findings']:
                        print(safe_text(f"[{f['category']}] {human(f['size'])} {f['path']}"))
                        print(safe_text(f"  {f['action']}: {f['reason']}"))
                        for line in f['evidence'].splitlines():
                            print('  ' + safe_text(line))
            else:
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    parser.error('Interface exige terminal; use --report ou --json')
                if args.ui == 'fzf':
                    from fzf_ui import launch
                    launch(policy, args.projects, args.days, args.system, args.measure, not args.no_probes)
                else:
                    from audit_ui import display
                    curses.wrapper(display, policy, args.projects.expanduser(), args.days, args.system, args.measure, not args.no_probes)
        elif args.report:
            print(f"Somente leitura | {safe_text(root)} | Livre: {human(shutil.disk_usage(root).free)}")
            for path, size, status in scan(root):
                print(f"{human(size):>13}  {safe_text(path.name)}  [{status}]")
        else:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                parser.error("A interface exige um terminal. Use --report para saída textual.")
            curses.wrapper(tui, root)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired, curses.error) as exc:
        print(f"Falha: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
