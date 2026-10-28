# Plano

Desenho original. O código já saiu deste esboço. O estado vivo está em [progresso.md](progresso.md); o que ainda falta, em [pendencias.md](pendencias.md).

## Decisões já no código (2026-10-18)

- Layout real: `backend/app/` e `frontend/` (React + Tailwind, não HTMX e não app Node). CLI: `python -m backend.app.cli`. Banco: `data/state.db`.
- PDF com reportlab, não WeasyPrint nem template HTML.
- Data vem de `ANO/NroMes-NomeMes/Dia-NroMes` (ex.: `2026/01-Janeiro/04-01`). A busca não abre pasta fora desse padrão. MP4 numa pasta que a busca abre, mas fora do padrão, vira `skipped`, com aviso.
- Palestrante vem de foto local (OpenCV YuNet + SFace), não de InsightFace e não de LLM de visão. Cosseno ≥ 0.50 com margem sobre o segundo colocado vira `auto`; de 0.363 a 0.50, ou dois rostos próximos, vira `needs_review`; abaixo disso, `unknown` (`backend/app/services/faces.py`). Modelos em `data/models/`.
- Entrada das fotos: `data/speakers/fotos/Nome.jpg` (ou `.jpeg`). Recorte e embedding: `data/speakers/<id>.jpg`.
- `needs_review` não trava o lote e não consome `--max`. A aula é transcrita e o MP4 apagado antes de esperar a revisão.
- Download confere o tamanho esperado e o `md5Checksum` do Drive, quando o arquivo traz o hash.
- A LLM só gera o resumo do PDF, com o nome já resolvido.
- Transcrição com `openai-whisper` (não faster-whisper), modelo `medium`, idioma `pt`.
- Fila: `pending` antes de `failed`, da aula mais recente para a mais antiga. Modo automático (`/api/pipeline/start`) com pausa e cancelamento; três falhas seguidas o interrompem.
- Um vídeo por vez, com uma exceção: o próximo MP4 baixa em paralelo enquanto o Whisper trabalha.
- Cada resumo também vira uma nota Markdown para o Obsidian (`data/obsidian/` ou `OBSIDIAN_DIR`), com tags e links das passagens.
- A interface de manutenção é lista + painel (Aulas, Palestrantes, Revisar, Ajustes). O Painel abre o item que precisa de atenção.
- A varredura do Drive não roda em todo lote. Um checkpoint (`drive_index` em `data/runtime.json`) guarda o token da Changes API. Sem alteração na pasta configurada, a fila segue no SQLite. `POST /api/pipeline/sync` força a busca. Pastas fora de `ANO/NroMes-NomeMes/Dia-NroMes` não são abertas, então MP4 nelas não vira `skipped`.

O texto abaixo é o rascunho de quando o repositório estava vazio. Foi mantido como histórico; onde ele discordar da lista acima, vale a lista.

---

O repositório está vazio, então o plano parte de um projeto novo. A ideia é um **pipeline sequencial em Python**, com registro local para não reprocessar, e uma execução diária depois do lote inicial.

## Objetivo

Processar vídeos de EBD no Google Drive **um de cada vez**: listar MP4s, baixar, transcrever com Whisper, extrair palestrante/data, gerar PDF de resumo, gravar o controle, enviar transcrição + PDF de volta à mesma pasta do Drive e apagar o MP4 local.

## Arquitetura

Módulos independentes, orquestrados por um único comando:

```
auto-ebd/
  src/auto_ebd/
    cli.py                 # python -m auto_ebd run | status
    pipeline.py            # orquestra 1 vídeo por vez
    drive.py               # listar, baixar, enviar
    transcribe.py          # Whisper
    identify.py            # palestrante + data
    summarize.py           # LLM + PDF
    registry.py            # SQLite (já processado)
    lock.py                # impede duas execuções simultâneas
  templates/resumo.html    # template do PDF
  prompts/resumo.txt       # prompt da aula
  config.toml
  data/state.db
  work/                    # downloads temporários
```

Fluxo por vídeo:

```
Drive (MP4) → download local → Whisper → identificar palestrante/data
→ LLM + template → PDF → gravar SQLite → upload .txt + .pdf no Drive
→ apagar MP4 local
```

Só inicia o próximo vídeo depois que esse ciclo termina (sucesso ou falha registrada).

## Controle de “já processado”

SQLite (`data/state.db`) com um registro por arquivo do Drive (`drive_file_id` como chave estável; nome de arquivo pode repetir).

Campos:

| Campo | Uso |
|---|---|
| `drive_file_id` | identidade do vídeo no Drive |
| `drive_folder_id` / `drive_path` | diretório do vídeo |
| `video_filename` | nome do MP4 |
| `transcript_filename` | nome do `.txt` |
| `summary_filename` | nome do `.pdf` |
| `speaker` | palestrante |
| `lecture_date` | data da palestra |
| `summary_text` | resumo em texto |
| `transcribed` | flag |
| `summarized` | flag |
| `uploaded` | flag |
| `status` | `pending` / `downloading` / `transcribing` / `summarizing` / `uploading` / `done` / `failed` |
| `error` | última falha |
| `updated_at` | auditoria |

Regras de idempotência:

1. Não processar se `status = done`.
2. Antes de baixar, conferir no Drive se já existem transcrição e PDF com o padrão de nome; se existirem, só sincronizar o registro e pular.
3. Se a execução cair no meio, retomar pelo estágio: transcrição pronta → não rodar Whisper de novo; PDF pronto → só enviar; enviado → só apagar o MP4 local.
4. Lock de arquivo (`fcntl`) para duas crons não rodarem juntas.

## Google Drive

- **OAuth do usuário** (pasta pessoal) ou **service account** com a pasta compartilhada.
- Listagem recursiva da pasta configurada, filtrando `video/mp4` (e extensão `.mp4`).
- Download com espera até o arquivo local ter o tamanho esperado (`contentLength`) e hash, se o Drive fornecer.
- Upload da transcrição e do PDF **na mesma pasta** do MP4 (`parents` = pasta do vídeo).
- Nomes sugeridos: `Aula-YYYY-MM-DD-palestrante.txt` e `.pdf`, ou `{nome-do-video}.txt` / `{nome-do-video}-resumo.pdf`.

Checagem de disco antes de baixar: se não houver espaço (tamanho do vídeo + margem), abortar aquele item e registrar `failed` sem corromper o lote.

## Whisper

- **Whisper** local. O plano previa faster-whisper; o código usa `openai-whisper` (ver decisões no topo).
- `ffmpeg` para áudio.
- Idioma padrão: `pt`.
- Modelo configurável (`medium` / `large-v3`); começar em `medium` e subir se a qualidade da transcrição for fraca.
- Saída: texto corrido + timestamps leves para o identificador de palestrante.
- Transcrição em `work/<id>/transcript.txt`. O MP4 só é apagado **depois** do upload bem-sucedido.

## Identificação do palestrante e da data

Script `identify.py` combina, nesta ordem:

1. **Metadados do Drive** (`createdTime` / `modifiedTime`) como data candidata.
2. **Nome do arquivo / pasta**, se houver padrão (ex.: `2024-03-10 - João Silva.mp4`).
3. **Início da transcrição** via LLM: “quem palestra e em que data, se citado”.

Resultado estruturado (`speaker`, `lecture_date`). Se a IA não achar, gravar `speaker = "Desconhecido"` e a data do Drive, para revisão posterior.

## Resumo em PDF

1. Prompt em `prompts/resumo.txt` (contexto EBD, tom pastoral/didático, seções fixas).
2. LLM (OpenAI, Anthropic ou modelo local via API compatível) recebe transcrição + palestrante + data.
3. JSON/markdown interno com seções do template, por exemplo:
   - título da aula
   - palestrante e data
   - texto-base / passagem
   - pontos principais
   - aplicações práticas
   - conclusão
4. Renderização HTML (`templates/resumo.html`) → PDF (WeasyPrint ou equivalente).

O PDF e o `.txt` sobem juntos; o texto do resumo também fica no SQLite.

## Execução: lote inicial e rotina diária

Um único comando: `python -m auto_ebd run`

- Pega **no máximo 1** vídeo pendente por vez.
- Em um `run` longo (lote histórico), o loop continua no mesmo processo, sempre um vídeo de cada vez, até não haver pendentes ou até um limite opcional (`--max 1` na cron diária).
- Cron / systemd timer **1x ao dia** (ex. 02:00):
  - `auto-ebd run --max 1` se a expectativa for no máximo um vídeo novo por dia, ou sem `--max` se puder chegar um lote.
- `auto-ebd status` lista pendentes, em andamento e falhas.

Como os arquivos são grandes, a cron diária não baixa em paralelo e não deixa MP4 antigo em disco: no `finally` de cada item, se o upload concluiu, remove o local.

## Configuração

`config.toml` (sem secrets no git):

- ID da pasta raiz no Drive
- pasta local `work/`
- modelo Whisper
- provedor/modelo da LLM
- caminho do prompt e do template
- idioma
- agendamento só documentado (o timer fica no sistema)

Credenciais: `.env` / arquivo OAuth fora do repositório.

## Tratamento de falhas

- Download incompleto: apagar parcial e marcar `failed` (retry no dia seguinte).
- Whisper/LLM/upload: status `failed` + `error`; não marca `transcribed`/`summarized` se a etapa não terminou.
- Retry só de etapas faltantes.
- Logs em `data/logs/AAAA/MM/DD/auto-ebd.log` (e `erros.log` no mesmo dia) + stdout para o journald/cron.

## Ordem de implementação sugerida

1. CLI + SQLite + lock  
2. Integração Drive (listar / baixar / enviar / apagar local)  
3. Whisper sequencial  
4. Identificação palestrante/data  
5. Prompt + PDF  
6. Pipeline completo + idempotência  
7. Timer diário + `status`

## Decisões já assumidas

- Python, não um serviço web: é um worker em lote.
- Um processo, um vídeo, sem fila Redis.
- Estado local em SQLite (simples de backupiar junto com o repo de dados).
- Whisper na máquina (sem enviar o MP4 para API de transcrição).

Quando quiser, o próximo passo é implementar nessa ordem — começando pelo esqueleto do projeto, Drive e o registro SQLite. Se já houver pasta do Drive, padrão de nome dos vídeos ou modelo de LLM/Whisper preferidos, encaixo isso no `config.toml` na implementação.


A data da palestra deve sair **do caminho no Drive**, não da IA. O padrão `ANO/NroMes-NomeMes/Dia-NroMes/*.mp4` já identifica o dia com precisão.

## Estrutura

```
<pasta raiz EBD>/
  2026/
    01-Janeiro/
      04-01/
        20260104_090354.mp4
```

Interpretação:

| Nível | Exemplo | Significado |
|---|---|---|
| `ANO` | `2026` | ano |
| `NroMes-NomeMes` | `01-Janeiro` | número do mês + nome |
| `Dia-NroMes` | `04-01` | dia + número do mês |
| arquivo | `20260104_090354.mp4` | vídeo da aula |

`lecture_date` = `2026-01-04` a partir de `2026/01-Janeiro/04-01`.

Validação: o mês de `Dia-NroMes` deve coincidir com `NroMes`. Se não coincidir, registrar aviso e usar `ANO` + `Dia-NroMes`. Nome do mês divergente só gera aviso.

## O que muda no pipeline

**Listagem:** recursiva a partir da pasta raiz, só arquivos `.mp4` em pastas que casam com `\d{4}/\d{2}-NomeMes/\d{2}-\d{2}`. Pastas fora do padrão entram em `skipped`, sem Whisper.

**Identificação:**

1. Data ← caminho (`ANO/NroMes-NomeMes/Dia-NroMes`).
2. Palestrante ← foto local. O nome do MP4 não define o palestrante.
3. `drive_path` gravado como `2026/01-Janeiro/04-01`.

**Upload:** transcrição e PDF na **mesma pasta do dia** (`Dia-NroMes`), ao lado do MP4:

```
2026/01-Janeiro/04-01/
  20260104_090354.mp4
  20260104_090354.txt
  20260104_090354-resumo.pdf
```

**Já processado:** além do `drive_file_id`, considerar processado se na pasta do dia já existirem `.txt` e `-resumo.pdf` (ou o padrão que combinarmos). Vídeos novos em `ANO/NroMes-NomeMes/Dia-NroMes` (ou um segundo `.mp4` no mesmo dia) entram na fila.

**Rotina diária:** listar a raiz, montar o conjunto de MP4s, cruzar com o SQLite. O caso típico é um único `Dia-NroMes` novo; o worker ainda processa **um MP4 por vez**.

**Nome sugerido dos artefatos** (estável para idempotência):

- `{stem-do-mp4}.txt`
- `{stem-do-mp4}-resumo.pdf`

Assim dois vídeos no mesmo dia não se sobrescrevem.

O palestrante vem da foto local. A data não depende da IA.

Isso já está em `backend/app/services/drive.py`: a listagem só aceita folhas `ANO/NroMes-NomeMes/Dia-NroMes/*.mp4` e `parse_lecture_date` preenche a data antes do resumo.

A identificação deixa de depender só do áudio: cada palestrante entra em um cadastro com foto, o pipeline compara um frame do vídeo com esse álbum, e o frontend serve para cadastrar, conferir e corrigir.

## Cadastro de palestrantes

Cada palestrante tem:

- nome
- foto de referência (rosto nítido, de frente)
- opcional: apelido, igreja/função
- ativo/inativo

Fotos em disco, vinculadas ao cadastro:

```
data/speakers/
  <id>-joao-silva.jpg
  <id>-maria-souza.jpg
```

O SQLite (ou o mesmo banco do pipeline) guarda `speakers` e o `lecture` aponta para `speaker_id`. Sem cadastro, a IA não tem com quem comparar.

## Como a identificação por imagem funciona

No vídeo, depois do download e **antes ou em paralelo** ao Whisper:

1. Extrair alguns frames (início da fala, 1–2 minutos, alguns pontos no meio) com `ffmpeg`.
2. Detectar o rosto principal.
3. Comparar com as fotos do cadastro (YuNet + SFace, no código). Isso é mais estável do que mandar o frame para um LLM de visão e pedir “quem é”.
4. Se a similaridade passar do limiar → preenche `speaker_id` + nome.
5. Se ficar no meio → status `needs_review` e o frontend mostra o frame + os 3 mais próximos.
6. Se não houver match → `Desconhecido` + revisão.

A data continua vindo de `ANO/NroMes-NomeMes/Dia-NroMes`. O palestrante vem da **foto**, não do path. O nome no arquivo MP4, se existir, só entra como dica secundária.

O LLM do resumo **recebe o nome já resolvido** pelo cadastro; não inventa palestrante.

## Frontend (para não virar trabalho manual)

App web local (FastAPI + interface única), o mesmo processo pode servir API + UI. Objetivo: cadastro fácil, fila de revisão e catálogo das aulas.

### Telas

1. **Palestrantes**  
   Lista com foto, nome, quantas aulas.  
   Criar/editar: upload da foto, recorte simples do rosto, nome.  
   Sem isso, o matching não roda bem.

2. **Aulas**  
   Grade por `ANO/NroMes-NomeMes/Dia-NroMes`: thumbnail ou frame, palestrante, data, status (`pendente`, `transcrevendo`, `revisar palestrante`, `resumo pronto`, `erro`).  
   Abrir aula: player não é obrigatório (vídeo some do disco); mostrar frame usado na identificação, transcrição, PDF, links no Drive.

3. **Revisar identificação**  
   Fila só do que ficou duvidoso.  
   À esquerda o frame do vídeo; à direita o cadastro (busca + fotos).  
   Um clique associa, reprocessa só o PDF se o nome mudou, e some da fila.

4. **Painel**  
   Quantos vídeos no Drive, processados, pendentes, falhas, espaço em `work/`.  
   Botão “processar próximo” (além da cron diária).

Fluxo de pouco esforço: cadastrar fotos uma vez → o lote corre sozinho → só abrir **Revisar** quando o score for baixo → o resto (Whisper, PDF, upload, apagar MP4) segue automático.

## Modelo de dados extra

```
speakers
  id, name, photo_path, embedding (vetor), active, created_at

lectures  (o registro que já tínhamos)
  ... speaker_id, speaker_confidence, identification_status
      (auto | reviewed | unknown)
  frame_path          -- frame usado na UI de revisão
```

A flag `transcribed` e o resumo permanecem; `speaker` deixa de ser texto solto e vira FK para o cadastro.

## Encaixe no pipeline (ainda 1 vídeo por vez)

```
listar MP4 novos
→ baixar
→ extrair frames + matching com fotos
→ Whisper
→ LLM gera resumo já com o nome do cadastro
→ PDF (foto do palestrante no template)
→ upload txt+pdf na pasta Dia-NroMes
→ apagar MP4
```

Se `identification_status = unknown` e a política for “não gerar PDF sem palestrante”, a aula para em `needs_review` e a cron não trava os outros: o próximo MP4 segue. Depois da correção no frontend, um job curto gera o PDF e envia ao Drive.

## Stack sugerida (alinhada ao worker)

- Backend: FastAPI (pipeline + API + servir o front)
- Front: React + Tailwind em `frontend/` — visual limpo (lista + cards com foto)
- Matching: biblioteca de face embeddings, não LLM de visão no lote (barato, local, repetível)
- LLM: só resumo em PDF, com nome já amarrado

O frontend não substitui a cron; ele **organiza o cadastro e a exceção**. O trabalho pesado continua um vídeo por vez, sem reprocessar o que já está `done`.

Próximo passo natural de implementação: banco (`speakers` + `lectures`) + telas de palestrantes/aulas + matching por foto, e só então Drive/Whisper/PDF. Se quiser, começo por esse esqueleto (API + front + pasta de fotos).