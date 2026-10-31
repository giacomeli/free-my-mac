# Free My Mac

Ferramenta de auditoria de ambiente dev para macOS, com interface fzf, inventário
por ecossistema, análise de projetos e planos de limpeza selecionados manualmente.
Requer Python 3.9+ e fzf. O modo interativo requer fzf no PATH. Dependências só são instaladas por uma ação explícita.

## Começar pela manutenção, sem esperar uma auditoria completa

Execute **sem sudo**, na pasta do projeto:

```sh
sh free-my-mac.sh --maintenance
```

O menu fzf mostra ações nativas por ferramenta. Escolher uma ação exibe suas
consequências e solicita confirmação antes de alterar dados. Ferramentas ausentes
aparecem como ausentes. A saída dos comandos permanece visível, com indicação de
tempo a cada cinco segundos e limite de 30 minutos. Ctrl-C encerra o grupo de
processos; interrupção pode deixar alterações parciais, registradas em JSON.

1. Caches: npm, pip, uv, pnpm, Yarn, Conda, Composer, NuGet e Go.
2. Homebrew: cleanup, prévia de autoremove e desinstalação individual de fórmulas/casks.
3. Docker: imagens sem containers associados, cache de build ou containers parados.
   Não executa prune de volumes. Imagens locais e camadas graváveis podem ser perdidas.
4. Versões: pyenv, Conda/Miniforge, nvm, fnm, asdf, mise e rustup. A seleção é manual;
   os requisitos encontrados nos projetos são exibidos antes da confirmação.
5. Android: pacotes SDK, imagens e AVDs; Apple: dispositivos desligados e runtimes.
6. Cowork: desativação da preferência e remoção da VM, com Claude Desktop fechado.
7. Apple Intelligence: instalar a dependência, consultar, remover modelos ou reativar.

O fluxo individual de versões também abre diretamente:

```sh
sh free-my-mac.sh --maintenance versions --projects ~/Projects
```

Não é possível provar que uma versão ou app não é usado apenas pela data dos arquivos.
Alguns gestores excluem versões ativas/default da seleção; isso não cobre todos os
projetos, aliases ou ambientes externos. Casks com instaladores externos e destinos
não verificáveis exigem seu próprio desinstalador. Scripts/plugins de gerenciadores
instalados são executados pelo próprio gerenciador, não auditados por este projeto.

## Remover Apple Intelligence

O módulo usa a CLI externa [RemoveMacAI](https://github.com/omlahore/RemoveMacAI),
sem incorporar seu código privado nem instalar nada durante a auditoria:

```sh
# Se ainda não estiver instalada: mostra a ação, sem instalar
sh free-my-mac.sh --maintenance ai-install
# Instala via Homebrew, após confirmação
sh free-my-mac.sh --maintenance ai-install --apply
# Consulta modelos
sh free-my-mac.sh --maintenance ai-status
# Apresenta a ação de remoção
sh free-my-mac.sh --maintenance ai-remove
# Executa a remoção, após confirmação
sh free-my-mac.sh --maintenance ai-remove --apply
```

A CLI solicita remoção dos modelos pelo serviço Apple. O perfil de bloqueio de
downloads exige aprovação nos Ajustes do Sistema. A liberação dos arquivos pode
ser assíncrona: execute `ai-status` novamente para verificar. O retorno zero do
comando não é garantia de uma quantidade de GB recuperada. SIP desligado não é
pré-requisito desse mecanismo. Não são apagados modelos de Ollama/Hugging Face.
`--maintenance ai-restore --apply` reativa os recursos; novos downloads podem ser necessários.
Compatibilidade depende da versão instalada do RemoveMacAI e do macOS; o projeto
externo usa interfaces privadas da Apple. A instalação via Homebrew não fixa versão.

## Remoção individual avançada

Na auditoria, a opção avançada permite escolher um alvo por vez. Também existe CLI:

```sh
sh free-my-mac.sh --remove-path "$HOME/Library/Application Support/AppRemovido"
sh free-my-mac.sh --remove-path "$HOME/Library/Application Support/AppRemovido" --apply
```

O catálogo inclui filhos diretos de Application Support/Caches/Preferences,
instalações conhecidas de Conda, versões específicas de Python/.NET, fontes em
`/Library/Fonts`, logs antigos em `/Library/Logs` e uma lista de apps Apple opcionais.
Herd pode ser selecionado explicitamente: isso inclui configurações e possíveis
dados locais, não apenas PHP. Não há exclusão genérica de `/opt` ou `/usr/local`.

O executor exige digitar o nome do alvo, valida identidade/conteúdo, verifica
arquivos abertos via lsof, recusa links na raiz e conteúdo protegido. Ausência de
processos visíveis não prova que nenhum outro usuário/processo utilizará o alvo.
Resíduos são selecionados por você; ausência do aplicativo não prova que seus dados
são descartáveis. A exclusão é permanente.

Apps como Maps/Music só podem ser removidos se o volume já permitir escrita.
O script verifica essa condição e informa o bloqueio. **Desligar SIP, sozinho,
não torna o volume selado gravável.** Não há remount, alteração de authenticated
root ou criação de snapshot de boot. Isso também significa que o script não
promete remover apps do volume selado numa instalação padrão do macOS.

Quando necessário, apenas o executor de alvo individual solicita sudo; a interface
e os gestores continuam no usuário normal. Há uma segunda confirmação no helper
privilegiado. Registros privilegiados ficam em `/var/log/free-my-mac-remove-*.jsonl`.

Snapshots, banco de unified logging e atualizações protegidas do macOS ficam fora
da remoção genérica. Limpeza de disco não garante ganho de CPU/RAM; não alteramos
permissões globais, swap, Spotlight ou serviços em massa.

## Abrir a auditoria

```sh
cd /caminho/para/free-my-mac
sh free-my-mac.sh --system --projects ~/Projects --days 60
```

A auditoria não apaga arquivos. `--system` inclui /opt, /usr/local, fontes,
serviços e apps do sistema. `--days` define a janela de atividade; 60 é um padrão
ajustável. Use o caminho real dos seus projetos (~/projects também pode existir).

Adicione `--measure` para medir espaço alocado. Sem essa opção o inventário é
mais rápido e o tamanho aparece como indisponível. Pastas têm limite de 30 segundos
por medição. Milhares de entradas podem levar vários minutos. Os tamanhos de
pais/filhos não devem ser somados; APFS/clones e discos esparsos não permitem
inferir espaço recuperável pela soma dos tamanhos.

## Interface fzf (padrão)

A auditoria mostra etapa/item atual, tempo total e tempo na etapa. Ctrl-C cancela
ela e suas consultas em um processo separado. Não há percentual fictício: a
quantidade de arquivos é descoberta durante a leitura. Ao terminar, mostra
LEITURA CONCLUÍDA e abre o menu. Ao salvar um plano, a validação também mostra
progresso e pode ser cancelada.

1. **Explorar todos os achados:** busca por texto e prévia dos detalhes à direita;
   Enter ou Esc volta ao menu. Esta lista não permite marcar exclusões.
2. **Selecionar ações elegíveis por categoria:** escolha uma categoria ou Todas.
   Digite para filtrar; Tab marca; Ctrl-A marca todos os resultados filtrados;
   Ctrl-D desmarca os resultados filtrados; Enter adiciona ao conjunto selecionado.
   Sem marcações, Enter adiciona a linha atual, conforme o comportamento do fzf.
3. **Revisar seleção atual:** examinar os itens acumulados, sem executar nada.
4. **Salvar plano selecionado:** valida e grava JSON no diretório atual.
5. **Salvar relatório completo:** grava JSON no diretório atual.
6. **Desmarcar tudo** ou **Sair**. Esc no menu também sai.
7. **Manutenção:** abre as ações nativas acima.
8. **Executar seleção atual:** salva o plano, revalida e pede confirmação.
9. **Remoção individual avançada:** abre os alvos explicitamente suportados.

Selecionar novamente acumula itens; use Desmarcar tudo para reiniciar a seleção.
O seletor inclui caches, node_modules elegíveis, builds reconhecidos, fontes do usuário, instaladores antigos e logs antigos. Builds exigem seleção explícita e não seguem o filtro de idade dos node_modules. Salvar não executa limpeza. Relatórios/planos privados
(0600) não sobrescrevem arquivos existentes. Os dados temporários da interface
ficam em diretório privado e são descartados ao sair; exportações são mantidas.

A interface anterior continua acessível com `--ui curses`; `--browse` mantém o
navegador de diretórios. O modo fzf exige um terminal interativo, sem sudo.

## Proteções

```sh
sh free-my-mac.sh --system --protect ~/Projects/meu-agente/artifacts --protect ~/modelos
```

`--protect` é repetível. As raízes .claude, .codex, .cursor, .ollama,
.cache/huggingface e sessões locais conhecidas do Claude são protegidas por padrão.
Nomes contendo sketchpad também são protegidos. Adicione as raízes reais dos seus
artefatos: não existe identificação universal de arquivos gerados por agentes.
Proteções bloqueiam a exclusão de ancestrais que contenham dados protegidos.

## Simular e executar um plano

Feche os gerenciadores e processos dos projetos selecionados. Uma execução exige
as mesmas opções --projects, --days e --protect usadas ao criar o plano.

```sh
sh free-my-mac.sh --plan free-my-mac-plan-NUMERO.json --days 60
```

O comando acima apenas valida/simula. Para executar, por iniciativa sua:

```sh
sh free-my-mac.sh --plan free-my-mac-plan-NUMERO.json --days 60 --apply
```

Será necessário digitar `APAGAR ITENS` em um terminal interativo. O executor não
aceita root. Valida alvos, propriedade, proteções, identidade e metadados da árvore;
se houver mudanças desde o plano, exige gerar outro. Não aceita comandos arbitrários
no JSON. Gera registro JSONL de início, resultado, espaço livre e falhas parciais.
A exclusão é permanente, sem promessa de rollback. Downloads/reinstalação podem
ser necessários; alterações manuais dentro de node_modules serão perdidas.
Não execute com processos escrevendo nos alvos: a validação não é um bloqueio
transacional do filesystem nem um detector completo de processos ativos.

node_modules só vira candidato com fontes antigas, lockfile e inspeção sem erro.
Dependências conhecidamente versionadas são bloqueadas. Isso é um indício de
inatividade, não prova de desuso: confira se o projeto é necessário offline e se
as dependências podem ser recuperadas. Monorepos e workspaces exigem revisão.

## Manutenção explícita do Cowork

```sh
sh free-my-mac.sh --cowork status
sh free-my-mac.sh --cowork disable
```

Status e simulação não alteram nada. Para aplicar após fechar Claude Desktop:
`sh free-my-mac.sh --cowork disable --apply`. Há confirmação interativa, backup
privado do JSON e remoção restrita a vm_bundles; sessões e MCP são preservados.
O comportamento da preferência local depende da versão. Veja o
[guia compartilhável](docs/guia-cowork.md), que explica limites, reversão e validação.

## Relatórios e crescimento

```sh
sh free-my-mac.sh --report --system --no-probes
sh free-my-mac.sh --json --system --measure --save inventario-1.json
sh free-my-mac.sh --json --system --measure --save inventario-2.json
sh free-my-mac.sh --compare inventario-1.json inventario-2.json
```

`--no-probes` pula consultas aos gerenciadores e Docker, mas continua lendo metadados
locais de apps pelo plutil do macOS. A comparação considera apenas caminhos medidos
nos dois relatórios; valores desconhecidos não viram zero.
Docker remoto/overrides são bloqueados; não iniciamos daemons. Um socket Unix pode ser um encaminhamento local; confira o endpoint mostrado antes de confirmar qualquer prune.

O navegador antigo continua disponível:

```sh
sh free-my-mac.sh --browse --path ~/Library
```

## Testes

```sh
python3 -m unittest discover -s tests -v
sh -n free-my-mac.sh
```

Exclusões nos testes usam exclusivamente diretórios temporários. Não é necessário
sudo. Não foi executada limpeza nos dados reais do Mac durante desenvolvimento.
