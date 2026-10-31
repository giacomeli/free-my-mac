# Desativar a VM local do Cowork mantendo o aplicativo instalado

Este guia não contém nomes de usuários, projetos ou caminhos privados. Nomes públicos
do Claude e das ferramentas são mantidos para identificar o procedimento.

## Compatibilidade e efeito esperado

A Anthropic documenta `secureVmFeaturesEnabled` como controle de acesso ao Cowork
na [configuração empresarial](https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop).
Isso não garante suporte idêntico à chave em `preferences` do JSON local em todas
as versões. O procedimento abaixo implementa esse ajuste local e deve ser validado
ao reabrir o aplicativo e após atualizações. Não promete impedir todo download futuro.

O script não desinstala Claude Desktop nem toca a instalação do Claude Code,
`~/.claude`, sessões locais ou configurações MCP. Não podemos garantir que todas
as integrações e recursos do aplicativo se comportem igual após desativar Cowork.

## Verificar sem alterar

Execute na pasta do script, sem sudo, substituindo `<script>.sh` pelo inicializador:

```sh
sh <script>.sh --cowork status
sh <script>.sh --cowork disable
```

O primeiro mostra o estado da preferência e a existência do bundle. O segundo
simula a alteração. Se a preferência já for false e o bundle estiver ausente,
retorna “já aplicado” e não cria arquivos nem repete a exclusão.

## Aplicar explicitamente

1. No Claude Desktop, use Developer > Open App Config File, se disponível, para
   conferir o arquivo. A implementação espera o caminho padrão
   `~/Library/Application Support/Claude/claude_desktop_config.json`.
2. Encerre o aplicativo com Cmd+Q. Não mantenha sessões de VM em execução.
3. Confira a simulação e execute:

   ```sh
   sh <script>.sh --cowork disable --apply
   ```

4. Digite a confirmação solicitada. O script cria backup privado da configuração,
   preserva as outras chaves e grava `preferences.secureVmFeaturesEnabled = false`.
5. Remove permanentemente apenas `~/Library/Application Support/Claude/vm_bundles`,
   se existir. Bloqueia links, arquivos abertos, caminhos protegidos e mudanças
   detectadas durante a operação. Não esvazia a Lixeira e não limpa a pasta Claude inteira.
6. Reabra o app e verifique chats e integrações. Rode `--cowork status` novamente.

O backup protege apenas o JSON, não o conteúdo da VM. Se a remoção falhar depois da
alteração da preferência, o comando informa a falha parcial e o caminho do backup.
Não reabra o app enquanto a operação estiver em andamento. Não existe bloqueio
transacional contra aplicativos iniciados simultaneamente.

## Alternativa manual

Com o aplicativo fechado, faça backup do JSON e defina somente esta propriedade,
preservando todas as outras:

```json
{
  "preferences": {
    "secureVmFeaturesEnabled": false
  }
}
```

O trecho é ilustrativo, não substitui o arquivo inteiro. Remova somente vm_bundles
pelo Finder. Mover para a Lixeira não libera necessariamente espaço: remova
definitivamente apenas esse item quando tiver certeza, sem esvaziar outros itens.

## Reversão e verificação após atualização

Com o app fechado, restaure a preferência anterior a partir do backup, preservando
mudanças posteriores no restante da configuração. A VM excluída não é restaurada
pelo backup; retomar o recurso pode exigir provisionamento/download pelo aplicativo.
Se uma atualização recriar o bundle ou ignorar a preferência, reavalie a versão e
as opções oficiais de armazenamento antes de repetir a remoção.

Não compartilhe o JSON real nem seu backup: eles podem conter credenciais MCP.
