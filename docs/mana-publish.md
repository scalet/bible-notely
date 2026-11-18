# Aula no Maná Bíblico a partir do bible-notely

O passo `publish` sobe **somente** a versão reduzida (`{stem}.compressed.mp4`) ao Bunny Stream com a chave da biblioteca (`BUNNY_STREAM_API_KEY`) e, se `MANA_PUBLISH_ENABLED=true`, grava a aula na API do Maná. O título segue o padrão da biblioteca (`IBQB - EBD - 15/11/26 - …`). Há no máximo um vídeo de cada tipo por dia (EBD, Culto da Manhã, Culto da Noite). `cli publish --dry-run` mostra o envio sem chamar as APIs. A flag nasce desligada. O corpo é sempre rascunho. Uma aula que uma pessoa já publicou não é alterada.

Fonte do contrato: `scalet/mana-biblico`, `main` com login Google no Cloud Run, e o pull request [#3](https://github.com/scalet/mana-biblico/pull/3) (`cursor/bunny-stream-32e6`), em 2026-11-17.

## O que a API já faz

Rotas de admin, prefixo `/api`:

| Método | Caminho | Uso |
|---|---|---|
| `POST` | `/api/auth/session` | Troca um ID token do Google por um `session_token` |
| `GET` | `/api/admin/speakers` | Lista palestrantes (`speaker_id`, `name`) |
| `POST` | `/api/admin/lessons` | Cria a aula |
| `PUT` | `/api/admin/lessons/{lesson_id}` | Substitui os campos da aula |
| `GET` | `/api/admin/lessons/{lesson_id}` | Lê a aula, inclusive `status` |
| `PUT` | `/api/admin/lessons/{lesson_id}/bunny-video` | Grava só o `bunny_video_id` |

`LessonInput` (`backend/server.py`):

| Campo | Regra |
|---|---|
| `title` | Obrigatório |
| `date` | `AAAA-MM-DD` |
| `type` | `ebd`, `service` ou `other`. Não existe `culto`. Culto da manhã e da noite saem como `service`. |
| `speaker_name` | O servidor ignora o texto e grava o nome do cadastro. |
| `speaker_id` | Obrigatório. Tem de ser um palestrante que já existe. |
| `youtube_url` | Opcional. Vazio zera `youtube_video_id` num `PUT`. |
| `bunny_video_id` | Opcional. GUID do Bunny (UUID). |
| `verses` | Lista de strings |
| `body` | Markdown |
| `status` | `draft` ou `published` |

`build_lesson_fields` recusa a criação sem `speaker_id`. Se `status` é `published` e o texto está vazio, também recusa. No `PUT`, o `status` do corpo é gravado: `draft` apaga `published_at`.

O cliente deste repositório manda só `draft`. Antes de um `PUT`, ele faz `GET`. Se `status` é `published`, não envia `PUT`, não envia `POST` e não chama `PUT .../bunny-video`. A resposta de criação que já volta `published` também não é seguida de outra escrita.

Uma aula rascunho que uma pessoa já editou (título, corpo, versículos, tipo, data, palestrante ou status diferentes do último envio) também não é sobrescrita. `cli publish --force` só ignora o limite de um vídeo do mesmo tipo no mesmo dia.

O cliente envia `Idempotency-Key: {drive_file_id}` no `POST`/`PUT` de aula e persiste o rascunho em `state.json` antes do `POST`, para não criar outra aula se a execução cair no meio. A interface `ManaLessons.upsert_lesson(drive_file_id, …)` já está pronta para o futuro `PUT /api/pipeline/lessons/{drive_file_id}` (ligue `MANA_PIPELINE_LESSONS`). Até essa rota existir, o cliente continua em `/api/admin/lessons`.

O `speaker_id` vem de `data/publish/speakers.json` ou de `MANA_SPEAKER_MAP` (JSON `{"Gerson": "speaker_abc"}`). O arquivo entra primeiro; a variável cobre o mesmo nome. Sem mapa, o cliente chama `GET /api/admin/speakers` e casa o nome sem acento e sem diferença de maiúsculas. Dois cadastros com o mesmo nome interrompem a aula, em vez de escolher um.

Exemplo do `POST /api/admin/lessons`:

```json
{
  "title": "A fé que opera",
  "date": "2026-01-04",
  "type": "ebd",
  "speaker_name": "Gerson",
  "speaker_id": "speaker_abc",
  "bunny_video_id": "11111111-2222-3333-4444-555555555555",
  "verses": ["João 3:16", "Romanos 5:3-5"],
  "body": "## Texto-base\n\nJoão 3:16\n",
  "status": "draft"
}
```

Num `PUT` de rascunho que já tem YouTube, o cliente repete `youtube_url` como `https://www.youtube.com/watch?v={youtube_video_id}` para o servidor não apagar o vídeo. Aula publicada não chega nesse `PUT`.

## Autenticação que existe hoje

Não há token de integração nem conta de serviço aceita por essas rotas.

1. O navegador manda `POST /api/auth/session` com `{ "id_token": "<ID token do Google>" }`.
2. O servidor valida o token com `google.oauth2.id_token.verify_oauth2_token` e o `GOOGLE_CLIENT_ID`.
3. Gera `session_token` (`secrets.token_urlsafe(32)`), grava em `user_sessions` e define `expires_at` para daqui a 7 dias.
4. As rotas seguintes aceitam o cookie `session_token` ou `Authorization: Bearer <session_token>`.
5. `admin_user` ainda exige usuário `approved` com papel `admin` (e-mail em `ADMIN_EMAILS` ou `SUPER_ADMIN_EMAIL`).

`MANA_SESSION_TOKEN` é esse `session_token`. É a sessão de uma pessoa. Expira em 7 dias e pode publicar, editar e apagar aulas. Não é credencial de máquina. Sem ele, mesmo com `MANA_PUBLISH_ENABLED=true`, o cliente não faz pedido HTTP ao Maná.

## Credencial de máquina que ainda falta

Para a cron gravar rascunho sem a sessão de uma pessoa, a API precisa de um destes dois caminhos, e de nenhum outro poder:

1. **Segredo só das rotas de rascunho.** Comparar em tempo constante um segredo de ambiente (por exemplo `PIPELINE_API_TOKEN`) com `Authorization: Bearer`. Esse segredo não pode cair em `current_user` / `user_sessions`, senão vira sessão de admin. Ou:
2. **Conta de serviço do GCP.** O script apresenta um identity token do Google com audience igual à URL do Cloud Run. A API confere o e-mail da conta numa lista (por exemplo `PIPELINE_SERVICE_ACCOUNT`). O papel de Invoker do Cloud Run não basta: hoje a aplicação ainda exige a sessão de usuário.

Nos dois casos o servidor precisa garantir:

- `POST` cria com `status` forçado a `draft` e ignora qualquer outro valor. Não preenche `published_at`.
- `PUT` de uma aula `published` responde sem gravar (409 ou 200 com o documento intacto). O cliente também não envia o `PUT`.
- `PUT` de um rascunho atualiza título, data, tipo, `speaker_id`, versículos, texto e `bunny_video_id`, e não muda `status` nem `published_at`.
- Idempotência por um id estável (o `drive_file_id` da gravação). Hoje a API não tem esse campo; o cliente guarda `lesson_id` em `data/publish/state.json`. Se esse arquivo se perde, um `POST` novo cria outra aula.

Até isso existir, o bible-notely não inventa um bearer fixo. Ou usa a sessão Google de `MANA_SESSION_TOKEN`, ou não chama a API.
