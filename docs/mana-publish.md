# Contrato: rascunho de aula a partir do bible-notely

O passo `publish` do bible-notely sobe o vídeo ao Bunny Stream e depois grava a aula no Maná Bíblico. A aula tem de nascer como rascunho. Quem publica é uma pessoa, na administração do site.

## Por que o cliente não usa a API que já existe

O Maná Bíblico (`scalet/mana-biblico`, `backend/server.py`, em 2026-11-17) já tem:

- `POST /api/admin/lessons` e `PUT /api/admin/lessons/{lesson_id}`
- autenticação por sessão do Google (cookie ou `Bearer` do `session_token`), não por um token fixo de integração
- tipo `ebd` | `service` | `other` (não existe `culto`)
- vídeo só como `youtube_video_id` / `youtube_url`
- `speaker_id` obrigatório, apontando para um palestrante já cadastrado
- `status` aceito como `draft` ou `published` — um cliente descuidado publicaria a aula

Não há campo `bunny_video_id` nem um upsert por id externo. Por isso o bible-notely **não** chama esses endpoints. O cliente do contrato abaixo só roda com `MANA_PUBLISH_ENABLED=true`. Com a flag desligada, o vídeo ainda pode ir para o Bunny; a aula não é enviada.

## Endpoint esperado

```http
PUT {MANA_API_URL}/api/admin/pipeline/lessons
Authorization: Bearer {MANA_API_TOKEN}
Content-Type: application/json
Accept: application/json
```

`MANA_API_URL` é a origem (`https://manabiblico.com.br`). Se ela já terminar em `/api`, o cliente não duplica o prefixo.

O `PUT` é um upsert. A chave de idempotência é `external_id` (o id do arquivo no Google Drive). Repetir a mesma chamada atualiza a mesma aula e não cria outra.

### Corpo

```json
{
  "external_id": "1abcDriveFileId",
  "title": "A fé que opera",
  "date": "2026-01-04",
  "type": "ebd",
  "speaker": "Gerson",
  "verses": ["João 3:16", "Romanos 5:3-5"],
  "body": "## Texto-base\n\nJoão 3:16\n\n## Pontos principais\n\n- Deus amou o mundo\n",
  "bunny_video_id": "guid-do-bunny",
  "status": "draft"
}
```

| Campo | Regra |
|---|---|
| `external_id` | Obrigatório. Está no Drive e não muda se o arquivo for renomeado. |
| `title` | Obrigatório. Título do resumo. |
| `date` | Obrigatório. `AAAA-MM-DD`. |
| `type` | `ebd` ou `culto`. Gravação antes das 10h é `ebd`; culto da manhã e da noite são `culto`. |
| `speaker` | Nome do palestrante. O servidor resolve o cadastro; se não achar, guarda o nome e deixa a aula em rascunho. |
| `verses` | Lista de referências (pode ser vazia). |
| `body` | Markdown do resumo. Pode ser vazio num rascunho. |
| `bunny_video_id` | GUID do vídeo no Bunny Stream, depois da codificação. |
| `status` | Sempre `draft`. Qualquer outro valor na criação responde 400 e não grava a aula. |

O cliente do bible-notely só envia `draft`. Se alguém alterar o payload para `published`, o cliente recusa antes do HTTP.

### Resposta

`201` quando a aula é criada, `200` quando já existia:

```json
{
  "lesson_id": "aula_abc123",
  "external_id": "1abcDriveFileId",
  "status": "draft",
  "bunny_video_id": "guid-do-bunny",
  "created": true
}
```

`created` é `true` só na primeira gravação daquele `external_id`.

### O que o servidor precisa garantir

- Criar a aula com `status = draft`. Não preencher `published_at`.
- Se `created` é `true` e `status` não é `draft`, o cliente trata como erro e não marca a aula como sincronizada. Na tentativa seguinte ele continua recusando enquanto a aula seguir publicada por causa desse primeiro erro.
- Se um administrador já publicou a aula na tela, um `PUT` posterior pode responder `status: published` e `created: false`. Nesse caso o servidor mantém `published` e só atualiza título, data, tipo, palestrante, versículos, texto e `bunny_video_id`. O cliente não pede para despublicar.
- `401` se o token não for de administrador.
- `400` se faltar título, data, tipo ou palestrante, se a data for inválida, se o tipo não for `ebd`/`culto`, ou se `status` for diferente de `draft` numa aula nova.
- `409` se o servidor recusar a escrita. Se o corpo trouxer `lesson_id`, o cliente guarda o id e não reenvia o vídeo. Sem `lesson_id`, a publicação da aula falha e o vídeo no Bunny permanece.

## Bunny Stream

O upload não passa pelo Maná.

1. `POST https://video.bunnycdn.com/library/{BUNNY_STREAM_LIBRARY_ID}/videos` com header `AccessKey` e `{"title": "..."}`. A resposta traz `guid`.
2. TUS em `https://video.bunnycdn.com/tusupload`. A assinatura é `SHA256(library_id + api_key + expiration_time + video_id)`, nos headers `AuthorizationSignature`, `AuthorizationExpire`, `LibraryId` e `VideoId`.
3. `GET /library/{id}/videos/{guid}` até `status` ser `4` (Finished), `7` ou `8`. `5` e `6` são falha. O cliente não cria a aula se a codificação falhar.

Com `PUBLISH_TRANSCODE_720=true`, o arquivo local vira 720p H.264 antes do TUS e o upload pede `EnabledResolutions: 720p` e `EnabledOutputCodecs: x264`.

O mesmo SHA-256 não gera outro `guid`. O mapa fica em `data/publish/state.json`.
