# Bible Notely

Pipeline local para aulas da Escola Bíblica Dominical (EBD) e cultos gravados no Google Drive.

A aplicação baixa **um vídeo por vez**, identifica o palestrante pela foto, transcreve com Whisper, gera um PDF de resumo com as passagens na Almeida Revista e Corrigida (ARC) e devolve `.txt` e `.pdf` para a mesma pasta do dia no Drive. Cada resumo também vira uma nota Markdown para o Obsidian.

Repositório: [github.com/scalet/bible-notely](https://github.com/scalet/bible-notely)

## O que ela faz

1. Lê a pasta da EBD no Google Drive (`ANO/NroMes-NomeMes/Dia-NroMes`).
2. Enfileira MP4s grandes o suficiente (1 GiB ou mais, por padrão).
3. Baixa o próximo vídeo para `work/`.
4. Extrai frames e compara o rosto com o cadastro local de palestrantes (OpenCV YuNet + SFace). Match fraco vai para revisão humana.
5. Transcreve com Whisper (`medium`, português). Enquanto isso, o próximo MP4 já pode começar a baixar.
6. Pede à LLM só o resumo (Gemini, Grok, Claude ou Copilot). Os versículos vêm da ARC, sem paráfrase.
7. Gera o PDF, a transcrição `.txt` e a nota do Obsidian.
8. Envia `.txt` e `.pdf` de volta à pasta do dia e apaga o MP4 local.

A interface (React) serve para cadastrar palestrantes, acompanhar a fila, revisar identificações e conectar o Drive. A API e o pipeline são Python (FastAPI).

## O que fica só na sua máquina

Nada disso entra no Git:

| Caminho | Conteúdo |
|---|---|
| `.env` | chaves de API, OAuth e IDs de pasta |
| `keys/` | JSON de cliente OAuth / conta de serviço |
| `data/` | banco, token do Google, logs, frames, modelos, cache da Bíblia, notas |
| `work/` | vídeos baixados, transcrições e PDFs em processamento |
| `.venv/` e `frontend/node_modules/` | dependências instaladas |

Use `.env.example` como modelo. Copie para `.env` e preencha localmente. **Não commite chaves, tokens, client secrets nem service accounts.**

## Pasta no Drive

```text
<raiz>/
  2026/
    01-Janeiro/
      04-01/
        20260104_090354.mp4
        20260104_090354.txt
        20260104_090354-resumo.pdf
```

A data da aula sai do caminho (`2026-01-04`), não do nome do arquivo. A busca só abre pastas nesse padrão.

## Requisitos

- Python 3.14 em `/usr/bin/python3.14` (o Whisper precisa de wheel do `torch`; o 3.15 do mise não instala).
- Whisper (`openai-whisper` no `requirements.txt`), modelo `medium`, idioma `pt`. Na primeira transcrição o modelo é baixado (cerca de 770 MB).
- `ffmpeg` e `ffprobe` no `PATH` — o Whisper usa para o áudio; a extração de frames também.
- Node.js para instalar e buildar a interface.
- Conta Google com a pasta das aulas e um cliente OAuth (aplicativo da Web).

## Subir o ambiente

```bash
/usr/bin/python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
cd frontend && npm install && cd ..
```

Cadastre os palestrantes antes de preencher as chaves. O cadastro não depende do Drive nem da LLM.

## Palestrantes

Na tela **Palestrantes**, nome e foto entram juntos. A foto vai para `data/speakers/fotos/`. Também dá para soltar arquivos nessa pasta:

```text
data/speakers/fotos/João Silva.jpg
data/speakers/fotos/Maria Souza | diaconisa.png
```

- O nome do arquivo, sem extensão, é o nome do palestrante.
- `_` vira espaço.
- Tudo depois de ` | ` vira a função, se o cadastro ainda não tiver uma.
- Formatos: jpg, png, webp. Retrato de frente, um rosto só.

Importar: botão **importar pasta** na tela, ou

```bash
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli speakers
```

A tela abre em **Palestrantes** enquanto o cadastro estiver vazio. **Ajustes** fica bloqueado até existir pelo menos um professor.

## Chaves (depois dos professores)

No `.env`:

- `GOOGLE_DRIVE_FOLDER_ID`: ID da pasta raiz, não a URL.
- `GOOGLE_CLIENT_ID` e `GOOGLE_CLIENT_SECRET`.
- `GOOGLE_REDIRECT_URI` deve ser `http://127.0.0.1:8000/api/drive/callback` e estar cadastrada no cliente OAuth.
- `AI_PROVIDER` e a chave do provedor (`GEMINI_API_KEY`, `GROK_API_KEY`, `ANTHROPIC_API_KEY` ou `COPILOT_API_KEY`). Isso só serve para o resumo.

## Subir a aplicação

Desenvolvimento (API em 8000, interface em 5173):

```bash
./scripts/dev.sh
```

Abra `http://localhost:5173` (não use `127.0.0.1:5173`). Cadastre os professores. Em **Ajustes**, conecte o Drive e salve a pasta raiz. Em **Painel**: **processar próximo**, **iniciar automático**, **pausar** e **cancelar**.

Só a API, com o front já buildado:

```bash
cd frontend && npm run build && cd ..
PYTHONPATH="$PWD" .venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

A interface fica em `http://127.0.0.1:8000`.

Linha de comando:

```bash
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli status
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli run --max 1
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli speakers
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli obsidian
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli cleanup
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli youtube-auth
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli youtube-upload --max 1
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli youtube-sync
```

Na primeira identificação o programa baixa os modelos de rosto para `data/models/` (cerca de 40 MB). O modelo do Whisper (`medium`) baixa na primeira transcrição.

## Notas do Obsidian

Cada resumo gera `data/obsidian/AAAA-MM-DD - {video}.md`. Para gravar direto no vault, defina `OBSIDIAN_DIR` no `.env`. A nota tem frontmatter, links `[[João 3.16]]` e o texto ARC das passagens. `cli obsidian` regenera todas a partir do banco.

## YouTube v2

Envio dos MP4s da pasta EBD no Drive para o YouTube **não listado**, com título `IBQB - EBD|Culto - DD/MM/AAAA - {título do estudo}` e descrição montada a partir do resumo gerado aqui. O vínculo pasta ↔ URL fica no SQLite (`lectures.youtube_url`), visível na tela **Aulas**.

### Local (nesta máquina)

1. Processe/resuma as aulas (`cli run`).
2. Em **Ajustes → YouTube**, navegue nas pastas do Drive e clique em **usar esta pasta** (só aulas resumidas dentro desse diretório entram no upload). Deixe vazio = raiz inteira.
3. Conecte o YouTube (token separado do Drive, em `data/youtube/token.json`):

```bash
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli youtube-auth
```

4. Envie vídeos (baixa do Drive, sobe ao YouTube, grava SQLite e `data/youtube/state.json`):

```bash
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli youtube-upload --max 1
```

Cerca de **6 uploads/dia** por cota padrão da YouTube Data API. MP4s grandes passam pelo disco local (`work/youtube/`).

5. Se precisar reaplicar URLs do state sem reenviar:

```bash
PYTHONPATH="$PWD" .venv/bin/python -m backend.app.cli youtube-sync
```

(`youtube-sync` lê `data/youtube/state.json` e, se existir, o `state.json` do Colab na pasta `auto-ebd-youtube` no Drive.)

### Colab (volumes muito grandes / rede Google)

1. `cli youtube-catalog` — grava `auto-ebd-youtube/catalog.json` no Drive.
2. [colab/upload_youtube.ipynb](colab/upload_youtube.ipynb) — upload no Colab.
3. `cli youtube-sync` — importa URLs para o SQLite.

Pré-requisitos: YouTube Data API v3 ativa, canal verificado para vídeos longos, mesmo `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` do Drive (escopos `youtube.upload` e `youtube` no login do `youtube-auth` ou no OAuth do Colab).

## Rotina diária

`deploy/bible-notely.timer` dispara o serviço às 02:00, um vídeo por execução:

```bash
sudo cp deploy/bible-notely.service deploy/bible-notely.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bible-notely.timer
```

Ajuste `WorkingDirectory` e `ExecStart` no unit para o caminho deste clone. Log em `data/logs/AAAA/MM/DD/bible-notely.log`. Erros vão para `erros.log` no mesmo dia. O arquivo de log não guarda token nem chave.

## Testes

```bash
PYTHONPATH="$PWD" .venv/bin/python -m unittest tests.test_pipeline_rules tests.test_speakers tests.test_size_and_exclusion tests.test_prefetch tests.test_pdf tests.test_logging tests.test_youtube_classify -q
```
