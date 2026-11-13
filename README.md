# Rastro · Track Metricz

Aplicação própria de atribuição e acompanhamento comercial de conversas do WhatsApp, para uso em `track.metricz.com.br`. Implementação independente, baseada no comportamento descrito no documento de referência, sem código ou ativos do Tintim.

## O que está implementado

- Clientes separados, usuários e perfis gestor, operador e leitura.
- Múltiplas instâncias Z-API por cliente.
- Z-API Omni: configuração de chave por canal e registro/atualização do próprio webhook com formato `Z_API`, Bearer e assinatura HMAC; gzip com limite de expansão e deduplicação.
- Facebook por cliente: credenciais criptografadas, OAuth com `ads_read`, seleção de conta autorizada, importação diária dos últimos 30 dias e atualização a cada hora pelo worker.
- Relatório por anúncio com campanha, conjunto, investimento, impressões, cliques e custo por contato identificado no WhatsApp.
- Webhook privado por instância, segredo aleatório de 256 bits armazenado como hash e rotação da URL.
- Fila de entrada persistida no banco antes de responder ao webhook.
- Mensagens recebidas e enviadas, deduplicação por mensagem, edição e ordenação pelo horário do provedor.
- Links de WhatsApp com UTMs, GCLID, FBCLID e um código diferente a cada clique.
- Atribuição por código recebido ou `externalAdReply` da Z-API; histórico de evidências, primeiro toque no painel.
- Lista de conversas, busca, filtros, paginação e mudança de etapa com auditoria.
- Vendas em reais com proteção contra duplo clique e ação separada de envio à Meta.
- Fila de Purchase para Meta Business Messaging, quando houver `ctwaClid`, WABA, dataset e credenciais válidos.
- Dashboard dos últimos 30 dias e exportação CSV de conversas com os mesmos filtros.
- Testes de isolamento, permissões, CSRF, ingestão, atribuição, valores e entrega de conversões.

O dashboard conta contatos novos pela primeira mensagem no período. Receita usa a data da venda. O funil mostra a etapa atual dos contatos novos, não a taxa histórica de passagem entre etapas. Cliques associados não significam pessoas únicas.

## Executar localmente no Windows

Precisa de Python 3.14. No PowerShell, dentro desta pasta:

```powershell
.\Start.ps1
```

Abra <http://127.0.0.1:8765>. O comando mostra uma senha aleatória para o usuário `demo` na primeira execução. Os contatos, vendas e anúncios são **fictícios**. Na segunda execução, a senha permanece a mesma. Para alterá-la, use `python manage.py changepassword demo` no ambiente configurado. O banco local fica em `data/` e não deve ser enviado ao GitHub.

Para testar a fila de webhooks localmente, execute em outro terminal:

```powershell
.\Start.ps1 -Worker
```

Use somente um worker com SQLite. Produção exige PostgreSQL. A demonstração é proibida quando `DEBUG=0`.

`Start.ps1` lê `.env.local` quando existir. Esse arquivo é privado e ignorado pelo Git. Mantenha `data/local-secret.txt`: ele protege tanto as sessões quanto as credenciais criptografadas. Trocar `SECRET_KEY` exige restaurar a chave antiga ou cadastrar as credenciais novamente. Faça backup do banco e da chave separadamente.

## Z-API Omni

1. Conecte o número no painel Omni por WhatsApp Web/QR Code.
2. Em WhatsApp / Z-API, cadastre uma conexão com provedor **Z-API Omni**, o ID do canal e o telefone conectado.
3. Salve a **Secret Key do workspace** (Segurança → Credenciais da API). O token exibido no canal não substitui essa chave.
4. Configure `PUBLIC_BASE_URL` com a origem HTTPS pública do app ou do túnel local e reinicie web/worker.
5. Use **Registrar webhook Omni**. O app adiciona um webhook `RECEIVED_AND_DELIVERY` em formato `Z_API`, com Bearer e HMAC. Outros webhooks permanecem intactos. Não use formato `DEFAULT` nesse receptor.
6. Envie uma mensagem a partir de outro número e confira a conversa e o estado do evento no app. Para anúncios CTWA, valide o recebimento real de `externalAdReply.sourceId` e `ctwaClid`; não é possível fabricar a origem de mensagens sem evidência.

Para testes locais, rode `python webhook_proxy.py` e aponte um túnel HTTPS para `http://127.0.0.1:8766`. O proxy só encaminha POSTs dos webhooks Omni ao app em 8765, sem disponibilizar login/admin pela internet. A URL temporária muda se o túnel for recriado: atualize `PUBLIC_BASE_URL`, reinicie web/worker e atualize o webhook no app. O computador, app, worker, proxy e túnel precisam permanecer ligados.

O segredo HMAC retornado pelo Omni é salvo criptografado. A assinatura usa timestamp, tópico, partição, offset e corpo descomprimido, conforme a documentação. Assinaturas com mais de cinco minutos são recusadas. A URL isoladamente não autoriza callbacks Omni: também são exigidos Bearer e assinatura.

## Facebook / Meta Ads

1. Abra **Facebook / Meta** dentro do cliente e salve App ID e App Secret. Alternativamente, salve um token com `ads_read` autorizado para os ativos desejados.
2. No painel do app da Meta, habilite o fluxo de login compatível e cadastre exatamente o callback exibido pelo Rastro em URIs OAuth válidos. Localmente, abra sempre a mesma origem usada para iniciar o fluxo; o túnel exclusivo de webhooks não serve como callback de login.
3. Clique **Autorizar pelo Facebook**, conclua o consentimento, busque as contas e selecione a conta deste cliente. Apps em desenvolvimento e acesso a contas de terceiros dependem das permissões e da configuração aprovadas na Meta.
4. O worker importa dados em nível de anúncio dos últimos 30 dias e repete a consulta a cada hora após uma sincronização bem-sucedida. Uma falha preserva os dados anteriores e pede nova tentativa no painel. Não há alterações de campanhas ou orçamento.
5. Abra **Anúncios**. A associação usa o ID do anúncio recebido no WhatsApp; cliques e mensagens sem referência não são promovidos a conversões atribuídas. Valores usam a moeda da conta. Os nomes de anúncios são os mais recentes do período. Datas de investimento seguem o fuso da conta Meta; mensagens seguem o fuso do app.

Um token expirado/revogado exige nova autorização. A desconexão remove o token local; a revogação definitiva também pode ser feita nas integrações do Facebook. Dataset, WABA e código de teste são opcionais na tela e servem apenas para conversões elegíveis, não para importar anúncios. `META_GRAPH_VERSION` fixa a versão usada (padrão `v25.0`, configurável). `META_REDIRECT_URI` é opcional para um callback canônico; `PUBLIC_BASE_URL` configura a origem dos webhooks.

## Implantação no EasyPanel

Use os serviços já criados no projeto **vizantu**. A fonte do app é o repositório privado no GitHub, branch `main`, build path `/`, builder **Dockerfile**, caminho `Dockerfile`.

Serviços:

| Serviço | Tipo | Configuração |
| --- | --- | --- |
| `supabase-rastro` | Compose Supabase | Instância exclusiva com PostgreSQL e Studio |
| `rastro` | App | Site e worker no mesmo serviço; Dockerfile; porta interna 8000 |

1. Use o PostgreSQL do `supabase-rastro`, separado do `supabase-vizantu`, pelo host interno do container `db`.
2. Copie as variáveis de `.env.example` para `rastro`, preenchendo `DATABASE_URL`, `DATABASE_SCHEMA=rastro` e uma `SECRET_KEY` aleatória.
3. Gere a chave com `python -c "import secrets; print(secrets.token_urlsafe(50))"`. Não use a chave de exemplo.
4. Conecte o repositório privado usando a integração GitHub já autorizada do painel. Se preferir Git/SSH, use uma deploy key restrita a leitura deste repositório.
5. Implante `rastro`. O comando `boot.sh` executa as checagens e migrations antes de iniciar Gunicorn e o worker. Se qualquer processo terminar, o container encerra e o EasyPanel pode reiniciá-lo.
6. Configure o domínio `track.metricz.com.br`, HTTPS, destino HTTP na porta 8000. O proxy deve fornecer `X-Forwarded-Proto: https`. Não publique a porta 8000 diretamente na VPS.
7. Crie o primeiro administrador com `python manage.py createsuperuser` no shell do container. Como alternativa de primeiro deploy, defina `DJANGO_SUPERUSER_USERNAME` e `DJANGO_SUPERUSER_PASSWORD` no ambiente do serviço web; o boot cria somente usuários ainda inexistentes. Remova essas duas variáveis após o primeiro login. Não existe senha padrão em produção.
8. Mantenha o comando padrão do Dockerfile. O worker inicia dentro do mesmo container; não crie outro serviço para ele.
9. Consulte `/health/`, abra o painel e faça o piloto com um cliente antes de cadastrar os demais.

O app não necessita de volume próprio: os dados persistem no PostgreSQL. Configure backup diário do banco e teste a restauração em um banco separado. Não configure cache de CDN para páginas autenticadas ou `/r/`. Não ative HTTP Basic Auth na frente dos webhooks Z-API.

### Studio do Supabase

Escolha o schema **rastro** no Table Editor para ver as tabelas do aplicativo. O boot cria esse schema antes das migrations, revoga seu acesso de `PUBLIC` e habilita RLS nas tabelas após as migrations. O Django usa a conexão SQL no servidor; as tabelas do app não devem ser adicionadas à lista `PGRST_DB_SCHEMAS` da Data API. O Studio permite administração com a credencial privada da instância; essas credenciais nunca são enviadas ao navegador do Track.

O Supabase Auth e os usuários do Django são sistemas diferentes. Os acessos à aplicação são geridos no Track/Django; a senha do Studio é reservada ao administrador. Não altere senhas de usuários Django diretamente no editor de tabelas.

O Gunicorn não habilita access log. Em outros logs/proxies, evite registrar o caminho completo de `/hooks/zapi/`, pois ele contém o segredo do webhook. Não publique `.env`, banco, payloads reais, senhas ou tokens no repositório.

## Conectar um cliente à Z-API

1. No app, crie um cliente com um identificador único, por exemplo `CLIENTE_01`.
2. Em **WhatsApp / Z-API**, adicione nome, ID da instância e número internacional somente com dígitos.
3. Copie a URL privada exibida uma única vez. Não é a URL das campanhas.
4. Na instância Z-API, configure o webhook **Ao receber — enviadas por mim** com essa URL. Ele deve entregar mensagens recebidas e enviadas; o app distingue `fromMe`.
5. Se a instância já atende n8n, CRM ou outro sistema, preserve a integração existente: faça o fan-out no fluxo atual e mantenha o segredo restrito aos sistemas autorizados. Não substitua uma URL em uso sem analisar esse fluxo.
6. Envie uma mensagem de teste pelo celular e confira seu estado na lista de webhooks e a conversa registrada. Depois responda pelo WhatsApp e confira a saída.

Na Z-API tradicional, o app não modifica a configuração nem envia mensagens. Não precisa armazenar token da instância ou Client-Token. A autenticação de entrada tradicional é uma URL secreta e a verificação da instância/número; **não é assinatura criptográfica da Z-API**. Quem obtiver essa URL pode falsificar callbacks. Rotacione em caso de exposição. O receptor Omni usa o fluxo assinado descrito acima.

Origem de anúncios usa `externalAdReply.sourceId` e `ctwaClid` quando fornecidos. O registro pode vir sem esses campos. O app nunca transforma FBCLID em CTWA Click ID nem atribui uma conversa só pela proximidade de horários.

## Piloto de rastreamento

1. Crie um link com origem/campanha e copie sua URL.
2. Abra a URL com `?utm_source=google&utm_medium=cpc&utm_campaign=teste&gclid=teste-local`.
3. Antes de enviar a mensagem, confira o código `[R:...]` no WhatsApp.
4. Envie a mensagem. O clique só deve aparecer associado depois que o webhook for processado.
5. Abra a conversa e confira origem, mensagem e etapa.
6. Registre uma venda; confira o valor e a trilha de auditoria.
7. Reenvie o mesmo callback no ambiente de homologação: a mensagem não deve duplicar.

O código é correlacionado somente na mesma instância e até 30 dias após o clique. Se for removido ou compartilhado com outra pessoa, a atribuição tem essa limitação. Bots/prévias podem gerar cliques. Contatos já existentes mantêm a primeira origem; outras evidências continuam no histórico.

## Conversões Meta

O envio é opcional e feito somente após ação explícita do gestor na venda. A integração não está habilitada só porque a Z-API está conectada.

Você pode cadastrar as credenciais de conversão na tela Facebook / Meta do cliente. Alternativamente, no ambiente do serviço, defina `META_GRAPH_VERSION` com uma versão suportada pela sua aplicação Meta, e:

```dotenv
META_CLIENTE_01_DATASET_ID=ID_DO_DATASET_BUSINESS_MESSAGING
META_CLIENTE_01_ACCESS_TOKEN=TOKEN_COM_PERMISSAO_NO_DATASET
META_CLIENTE_01_WABA_ID=ID_DA_CONTA_WHATSAPP_BUSINESS
META_CLIENTE_01_TEST_EVENT_CODE=CODIGO_DE_TESTE
```

Substitua `CLIENTE_01` pelo identificador exato criado no painel. Teste no Events Manager antes de remover o código de teste. Z-API por QR code não prova que o cliente tem WABA/dataset compatíveis; a elegibilidade e os IDs precisam ser verificados na Meta. Sem `ctwaClid`, o botão apresenta o motivo do bloqueio.

O envio usa um payload congelado e identificador estável da venda. HTTP 429 agenda até cinco tentativas. Timeout, erro de rede, HTTP 5xx ou worker interrompido levam à revisão manual, sem repetição automática: uma requisição sem resposta pode ter sido recebida pela Meta. Aceite pela API não garante atribuição ao anúncio. Não há botão de reenvio indiscriminado para resultados incertos.

## Operação e dados

`python manage.py runworker --once` processa até um webhook e um despacho, útil para diagnóstico. Eventos malformados ficam como `failed`, com o payload original preservado. Após corrigir a causa, `python manage.py retry_webhook ID` recoloca somente aquele webhook na fila. Identificadores `@lid` não são convertidos em telefones; precisam de tratamento conforme os dados reais da instância.

`python manage.py prune_events` mostra quantos payloads brutos processados têm mais de 30 dias; `--apply` os remove. Isso não apaga conversas ou vendas. A política de retenção das mensagens, exclusão por solicitação e permissões da equipe deve ser definida pela empresa antes de ampliar o uso. A auditoria é protegida na interface do app; o administrador do banco ainda pode modificá-la.

## Validação

```sh
python manage.py test crm
python manage.py check --deploy --fail-level WARNING
python manage.py makemigrations --check --dry-run
```

O workflow em `.github/workflows/check.yml` executa os testes com PostgreSQL no Linux, checa a configuração e constrói a imagem. O conjunto cobre isolamento e permissões, callbacks repetidos, timestamps fora de ordem, vínculo de cliques, edições, valores de venda, CSRF, limitação de login, CSV e estados de entrega Meta. Testes de API externa usam respostas simuladas; a integração real precisa do piloto.

## Próximas etapas do documento original

Este é o MVP transacional com integração de anúncios Meta. Ainda não inclui kanban, IA de vendas, onboarding Google, ROAS por anúncio, API pública/MCP, leitura de QR codes dentro do Rastro, pixel de navegador, webhooks de saída, relatórios assíncronos nem upload de conversões Google. A conexão por QR é feita no painel Omni. Áudio/mídia não são baixados ou transcritos. Histórico de mensagens anterior à conexão não é importado automaticamente. Alterações/exclusões e variantes de payload não documentadas precisam de validação adicional com a instância real. O registro do webhook e a autorização dos anúncios só podem ser confirmados com chaves válidas e uma mensagem real.

Referências consultadas: [Django 5.2 LTS](https://www.djangoproject.com/download/), [deploy Django](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/), [webhooks Z-API](https://developer.z-api.io/webhooks/on-message-received-and-delivery), [payloads Z-API](https://developer.z-api.io/webhooks/on-message-received-examples), [serviços App EasyPanel](https://easypanel.io/docs/services/app), [CAPI Business Messaging](https://developers.facebook.com/docs/marketing-api/conversions-api/business-messaging/).
