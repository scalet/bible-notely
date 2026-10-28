# Pendências

Atualizado em 2026-10-20 (noite). O que já foi feito está em [progresso.md](progresso.md). O desenho original está em [plan.md](plan.md).

## Em andamento

- [ ] Esvaziar a fila: 102 aulas `pending` (16 `done`, 34 `excluded`, 118 MP4 no último sync). A 134 (`20260906_190123.mp4`) é a próxima e vai baixar de novo. Rodar **iniciar automático** e acompanhar `data/logs/AAAA/MM/DD/erros.log`.
- [ ] Conferir mais matches reais de rosto; as fotos atuais são Gerson, Gil, Larice, Ricardo e Roberto. Há 2 aulas com identificação `unknown` (não estão na fila de revisão agora). Palestrante sem foto cai em `unknown`.
- [ ] Conferir as notas em `data/obsidian/` (tags e links de passagem) e apontar `OBSIDIAN_DIR` para o vault, se for o caso.

## Ambiente e repo

- [ ] Primeiro commit (ainda não há histórico).
- [ ] Subir o MCP `ai-memory` se for registrar o progresso nele (`~/.claude.json`, porta 49374). Estava desligado no levantamento de 2026-10-16.

## Testes

- [ ] Sync de pastas fora do padrão (`skipped`), lock do `/pipeline/continue`, retomada de PDF, pausa/cancelamento e o parser de passagens de `obsidian.py` (`parse_reference`). Data e ordem da fila estão em `tests/test_pipeline_rules.py`. Filtro de 1 GiB, `exclude`, `restore`, `DELETE /download`, listagem sem alteração e md5 do download estão em `tests/test_size_and_exclusion.py`. Prefetch em `tests/test_prefetch.py`. Match sem rosto em `tests/test_speakers.py`. Recuperação ociosa em `tests/test_pipeline_rules.py`.

## Fechado em 2026-10-20 (noite)

- MP4 de aula que não terminou apagado (`cli cleanup`); próximo lote baixa de novo.
- Logs em `data/logs/AAAA/MM/DD/`.

## Fechado em 2026-10-20

- Filtro de 1 GiB confirmado no banco: 34 `excluded`, sync em 118 MP4 (era 149).
- Fila de **Revisar** vazia.
- `transcribing` órfão: `recover_if_idle` no `cli status` / painel.
- `md5Checksum` no download, quando o Drive envia.
- Timeout Gemini/Claude 900s.

## Fechado em 2026-10-18

- Manutenção na interface: Aulas, Palestrantes, Revisar e Ajustes abrem um painel ao clicar, em vez de detalhe abaixo de uma tabela. O Painel leva direto ao item.
- Busca no Drive só quando a pasta configurada muda (Changes API + `drive_index` em `data/runtime.json`). Processar vídeo ou transcrever não relista a árvore. `POST /api/pipeline/sync` ainda força a busca.
- Conexão com o Drive (OAuth, `code_verifier` do PKCE) e primeiro sync: 149 MP4 listados.
- Modo automático, pausa, cancelamento e download adiantado.
- Fila da aula mais recente para a mais antiga.
- Exportação Markdown para o Obsidian e CLI `obsidian`.
- Log de erro diário.

## Fechado em 2026-10-17

O levantamento de 2026-10-16 pedia estes itens. Estão feitos; não reabrir sem motivo novo.

- `.venv` em Python 3.14, serviço e `scripts/run-daily.sh` apontando para `.venv/bin/python`, `package-lock.json` fora do ignore.
- Fila: `pending` antes de `failed`; falha não derruba o lote.
- `needs_review` não interrompe o lote e não consome `--max`. A aula é transcrita antes de parar e o MP4 é apagado; a revisão só gera PDF e envia.
- Lock compartilhado entre cron e `/api/pipeline/continue`.
- Espaço em `work/` antes do download; `video_size` gravado e usado como `expected_size`.
- Vários frames; retomada de transcrição; PDF só refeito se `summary_speaker_id` mudou; MP4 só some depois do upload.
- Pastas fora de `ANO/NroMes-NomeMes/Dia-NroMes` como `skipped`; aviso de mês divergente. Documentado no [README.md](../README.md).
- CLI `status`, log rotativo, painel (último sync e disco), detalhe da aula, CRUD de palestrantes e importação de `data/speakers/fotos/`.
- Identificação local com YuNet + SFace. A LLM não recebe frame. Não foi InsightFace: OpenCV já cobre detector e embedding, sem dependência extra pesada.
