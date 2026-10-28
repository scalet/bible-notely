# Memória do que foi feito e checklist

Atualizado em 2026-10-20 (noite). O que ainda falta está em [pendencias.md](pendencias.md). O desenho original está em [plan.md](plan.md).

## O que foi feito em 2026-10-20 (noite)

- MP4 incompletos em `work/` apagados para recomeçar o download: `20260906_190123.mp4` (12 GiB, prefetch que não entrou no Whisper) e `Encerramento_EBD_20250921.mp4` (~304 MiB de ~4,4 GiB). As aulas 134 e 2 seguem `pending`. TXT/PDF das aulas `done` ficaram. Comando: `cli cleanup`.
- Logs passam a `data/logs/AAAA/MM/DD/auto-ebd.log` e `.../erros.log`. Arquivos antigos na raiz de `data/logs/` não foram movidos.

## O que foi feito em 2026-10-20

Estado ao retomar (`cli status`): 15 `done`, 34 `excluded` (filtro de 1 GiB já aplicado no sync anterior), 102 `pending`, 1 `transcribing` órfã (aula 136, `Culto-da-manha.mp4`), 0 `needs_review`, 0 `failed`. Último sync: 118 MP4 no Drive. 5 palestrantes com foto (Gil, Gerson, Larice, Ricardo, Roberto).

- `recover_if_idle`: aulas presas em etapa voltam para `pending` quando o pipeline está ocioso. Roda no `cli status`, no `GET /api/pipeline` e no início do lote. A 136 saiu de `transcribing` sem precisar de um `run`.
- Download confere `md5Checksum` do Drive quando o arquivo traz o hash. Grava `video_md5` no banco. Hash errado apaga o parcial e conta como download incompleto (retry). Sem hash, segue só o tamanho.
- Timeout do Gemini/Claude no resumo subiu para 900s (o mesmo do Grok/Copilot). O `erros.log` tinha `httpx.ReadTimeout` em 180s.
- Testes: recuperação ociosa, listagem com md5, download com hash certo/errado.

## O que já existia antes desta continuação

- Backend FastAPI, SQLite, Drive (OAuth), Whisper, PDF (reportlab), LLM multi-provedor, pipeline com lock e CLI `run`.
- Frontend React com 5 telas: Painel, Aulas, Palestrantes, Revisar, Configurações. Em 2026-10-17 o CSS manual virou Tailwind.
- `deploy/bible-notely.service`, `deploy/bible-notely.timer` e `scripts/run-daily.sh`.
- Identificação de palestrante era via LLM de visão. Em 2026-10-17 passou a ser local.

## O que foi feito em 2026-10-18

Drive conectado e primeiras aulas processadas de verdade. Estado do banco no fim do dia (`cli status`): 149 MP4 no Drive, 7 `done`, 2 `needs_review`, 139 `pending` e 1 `transcribing` sobrado de execução interrompida (`recover_interrupted` devolve para `pending` na próxima rodada).

- Tamanho mínimo: só entram na lista MP4 com 1 GiB ou mais (`MIN_VIDEO_BYTES`, padrão 1073741824). A listagem do Drive já descarta os menores, e o sync marca como `excluded` os que já estavam no banco como `pending`, `failed`, `needs_review` ou `skipped`. Esses não voltam para a fila. `drive_video_count` passa a contar só os elegíveis.
- Excluir da importação: `POST /api/lectures/{id}/exclude` (status `excluded`, apaga o MP4 local) e `POST /api/lectures/{id}/restore` (volta para `pending`). Remover só o download: `DELETE /api/lectures/{id}/download` (apaga o MP4 em `work/`; transcrição, PDF e registro ficam). Recusam com 409 se a aula estiver em uso pelo pipeline. Botões na tela **Aulas**, no detalhe da aula.
- Modo automático: `POST /api/pipeline/start` processa a fila inteira (`UNLIMITED`), da aula **mais recente para a mais antiga** (antes a ordem era por id). `pipeline/run?max_items=N` segue para lotes limitados.
- Pausar e cancelar: `POST /api/pipeline/pause` termina a aula atual e para; `POST /api/pipeline/cancel` interrompe download e Whisper, e a aula volta para `pending`. Três falhas seguidas (`MAX_CONSECUTIVE_FAILURES`) param o modo automático. Botões no Painel.
- Download adiantado: enquanto o Whisper transcreve, o próximo MP4 pendente já baixa em paralelo (`_prefetch_video`). É o único paralelismo; transcrição, resumo e upload continuam um vídeo por vez. Falha no adiantado vira download normal depois. O Painel mostra o progresso.
- Download com até 3 tentativas se vier incompleto (`DriveIncomplete`); progresso por aula no `/api/pipeline`.
- Whisper: `openai-whisper` (não faster-whisper), modelo `medium` por padrão, GPU se houver CUDA. Progresso e cancelamento via barra do tqdm.
- Exportação para o Obsidian (`backend/app/services/obsidian.py`): cada resumo vira `data/obsidian/AAAA-MM-DD - {stem}.md` (ou `OBSIDIAN_DIR`), com frontmatter (título, palestrante, função, data, pasta, vídeo, passagens, tags) e links `[[Livro cap.vers]]`. O corpo cita a redação ARC das passagens, a mesma do PDF. Falha na exportação só loga; não derruba a aula. CLI `obsidian` regera todas as notas a partir do banco.
- Prompt `prompts/resumo.txt` pede `referencias` (todas as passagens citadas) e a tradução ARC (SBB).
- Log de erros: `data/logs/erros.log` (só ERROR+, com traceback). O `auto-ebd.log` rotativo continua com o restante.
- `data/runtime.json` guarda o que a tela de Ajustes altera (provedor de IA, pasta raiz) e a contagem de MP4 do último sync.
- Dependências: `requirements.txt` sem sobras; instalar o PyTorch CPU com `--extra-index-url https://download.pytorch.org/whl/cpu` se não houver GPU.

### Interface de manutenção

A lista longa com o detalhe embaixo da tabela saiu. Em **Aulas**, **Palestrantes**, **Revisar** e **Ajustes**, clicar no item abre um painel ao lado (no celular o painel ocupa a tela; Esc ou voltar fecha). O Painel lista o que precisa de atenção e abre a aula ou a revisão já no item.

- **Aulas:** busca e filtros (Todas, Atenção, Fila, Em curso, Prontas, Fora). O painel traz metadados, checklist (transcrita, resumo, no Drive), aviso, frame, palestrante, arquivos e fila. Excluir fica separado como ação perigosa.
- **Palestrantes:** filtros Todos, Sem rosto e Inativos. Novo cadastro e edição (nome, função, ativo, foto, exclusão) ficam no painel.
- **Revisar:** a sugestão de palestrante vem primeiro. Clicar só seleciona; confirmar atribui e segue. Se o item sair da lista, o próximo entra sozinho.
- **Ajustes:** seções Drive, IA e ambiente, uma de cada vez no painel.

### Busca no Drive

Cada lote (`processar próximo`, automático, timer) começava com uma varredura de todas as pastas, mesmo sem arquivo novo. O log de 2026-10-18 tem 6 varreduras de 70 pastas. O console do Google mostrou cerca de 22.500 solicitações; esse total também inclui o download (cada pedaço de 8 MiB conta), não só a busca.

Agora a varredura só ocorre se a pasta configurada mudou:

- Na primeira busca (ou se a pasta raiz mudar) o código lista o necessário e grava `drive_index` em `data/runtime.json` (token da Changes API, id da pasta e ids das pastas conhecidas).
- Nas seguintes, uma checagem de alterações substitui a varredura. Alteração fora da árvore conhecida é ignorada. Sem alteração, o log fica `Drive sem alteração; busca de pastas pulada` e a fila segue no SQLite.
- `POST /api/pipeline/sync` continua forçando a busca completa.
- PDF do resumo segue o modelo de `templates/pdf/EBD-20250921-resumo.pdf`: kicker, título, foto quadrada do recorte, uma linha `nome · função · data · pasta`, depois texto-base, pontos, aplicações e conclusão. Os versículos citados entram em itálico na redação ARC (SBB), buscada em `bibliaonline.com.br/arc/{livro}/{capitulo}` e guardada em `data/bible/arc/`. Capítulo inteiro só quando é o texto-base; o resto só entra com versículo. A LLM não inventa a redação — o programa busca e anexa o texto.
- Texto e PDF já vistos nessa listagem marcam a aula como pronta, sem uma segunda busca por arquivo.
- Aula já `uploaded` não é reconsultada no Drive durante o processamento. A transcrição só é buscada lá se `transcribed` estiver marcado e o `.txt` local não existir.
- Pastas que não são `ANO`, `NroMes-NomeMes` ou `Dia-NroMes` não são abertas. MP4 nelas deixa de entrar como `skipped`. MP4 em uma pasta que a varredura abre, mas fora do padrão, continua `skipped`.

Testes: `tests/test_size_and_exclusion.py` cobre a listagem pulada, a alteração dentro e fora da árvore, e as saídas lidas na mesma listagem.

## O que foi feito em 2026-10-17

Robustez do pipeline (itens 6–10 e 12–15 do levantamento) e parte de CLI, log e painel.

- Fila: `pending` vem antes de `failed`. Uma falha não trava o vídeo seguinte. A exceção fica no item (`status=failed`) e não derruba o lote.
- `needs_review` não interrompe o lote e não consome a cota de `--max`.
- `/api/pipeline/continue` usa o mesmo lock (arquivo + thread) que o cron.
- Checagem de espaço em `work/` antes do download (tamanho do vídeo + 512 MiB).
- `video_size` gravado no `Lecture` e passado como `expected_size` no download.
- Retomada: transcrição local ou já no Drive é reaproveitada; PDF só é refeito se o palestrante mudou (`summary_speaker_id`). O MP4 só é apagado depois do upload.
- Pastas fora de `ANO/NroMes-NomeMes/Dia-NroMes` entram como `skipped`, com aviso. Exemplo válido: `2026/01-Janeiro/04-01/20260104_090354.mp4`.
- `parse_lecture_date` registra mês divergente e usa `ANO` + `Dia-NroMes`. Nome do mês errado só avisa.
- Confirmar palestrante na revisão invalida só o PDF e reenvia, sem obrigar Whisper de novo se a transcrição existir.
- Vários frames (início, ~20% e meio) em vez de um único em 90s. O do meio fica na revisão.
- CLI `status`, log rotativo em `data/logs/auto-ebd.log`.
- Painel: vídeos do último sync do Drive e espaço livre em `work/`.
- Detalhe da aula: clique na linha mostra aviso, frame, transcrição e links do PDF e da pasta no Drive.
- Serviço e `scripts/run-daily.sh` apontam para `.venv/bin/python`.
- `.gitignore` não ignora mais `frontend/package-lock.json`.
- `.venv` criado com Python 3.14 (`/usr/bin/python3.14`) e `requirements.txt` instalado. O Python 3.15 do mise não tem wheel de `ctranslate2`, então o Whisper não instala nele.
- Testes: `tests/test_pipeline_rules.py` e `tests/test_speakers.py`.
- Identificação local com OpenCV YuNet + SFace. A LLM não recebe mais frames. Limiares: `auto` ≥ 0.50 (com margem sobre o segundo), `needs_review` ≥ 0.363, abaixo `unknown`.
- Pasta `data/speakers/fotos/`: o nome do arquivo cria ou atualiza o palestrante. O cadastro na tela pede a foto e grava o arquivo nessa pasta na hora.
- Interface em React + Tailwind (`frontend/`). Node só entra no `npm install` / `npm run build`. A API segue em FastAPI.
- Ordem de uso: professores antes das chaves. Ajustes, OAuth e o lote recusam se o cadastro estiver vazio.
- Como subir: [README.md](../README.md).
- Resumo Gemini: `gemini-2.5-flash` devolve 404 para projeto novo. A chamada que funciona é `gemini-3.5-flash-lite` com `GEMINI_API_KEY`. A conta de serviço `ai-auto-ebd-gemini@...` não consulta essa API (escopo insuficiente; Vertex AI não está habilitado).
- Log `auto_ebd.google` em `data/logs/auto-ebd.log`: login, conta OAuth, listagem, download, upload e recusa do Gemini. Sem token, código ou chave.
- O callback do Drive reusa o `code_verifier` salvo em `data/google_oauth_pkce.json`. Sem isso o Google responde `Missing code verifier` e o login cai em 500.
- A tela de desenvolvimento é `http://localhost:5173`. `127.0.0.1:5173` não abre porque o Vite escuta em localhost. O callback OAuth da API continua em `127.0.0.1:8000`.
- Documentos alinhados ao caminho real: [README.md](../README.md) e [plan.md](plan.md). Saída na pasta do dia: `{stem}.txt` e `{stem}-resumo.pdf`.

## Checklist

A lista do que falta fica só em [pendencias.md](pendencias.md), para não divergir. Já resolvidos: Drive conectado e primeiro lote real, `.venv` com Python 3.14 (não usar o `python3` 3.15 do mise), identificação local sem LLM de visão, recorte do rosto em `data/speakers/<id>.jpg`.

## Como retomar

```bash
cd /home/scalet/Documents/DEV/auto-ebd
# se precisar recriar: /usr/bin/python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHONPATH="$PWD" .venv/bin/python -m unittest tests.test_pipeline_rules tests.test_speakers tests.test_size_and_exclusion tests.test_prefetch tests.test_pdf tests.test_logging -q
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli status
```

O Drive já está conectado e há aulas processadas. Fila de revisão vazia. Para andar a fila (`103 pending`), use **iniciar automático** no Painel (**continuar** retoma depois de uma pausa) ou `cli run --max N`. O match só acerta se houver foto com rosto em `data/speakers/fotos/`.
