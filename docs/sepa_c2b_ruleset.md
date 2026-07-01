# Ruleset SEPA C2B — Validador (Fase 0, para revisão de negócio)

**Fonte:** Manual do Banco de Portugal *"C2B — Prestação de Serviços a Clientes /
Registo Normalizado (XML) SEPA"*, versão **03.01** (2016-11-21, 147 pág.).
**Âmbito PoC:** `pain.001.001.03` (Transferências a Crédito) e `pain.008.001.02`
(Cobranças / Débitos Diretos).

> ⚠️ **Rever antes de construir.** É um banco. O manual chegou como texto colado
> (com algum ruído de layout), por isso os itens marcados **`[CONFIRMAR]`** precisam
> de validação de negócio. As referências de página apontam para a numeração do manual.

## Como ler
- **Card. ISO → C2B:** cardinalidade ISO 20022 e, quando o manual a **aperta**, a
  regra C2B (ex.: `[0..1] → [1..1]` = opcional na norma mas **mandatório** para o BdP).
- Notação: `[1..1]` mandatório 1x; `[0..1]` opcional 1x; `[1..n]` mandatório 1+;
  `{OR..OR}` = escolha exclusiva entre tags.
- **Máx** = nº máximo de caracteres/dígitos. **Camada** = a que camada do validador a
  regra pertence: **E**=estrutura, **S**=schema/XSD, **N**=negócio (conteúdo).

---

## 1. Regras transversais

### 1.1 Conjunto de caracteres admitido — §3.3 (camada N)
Só são permitidos os caracteres latinos:
```
a-z  A-Z  0-9  / - ? : ( ) . , ' +  e espaço
```
- **Não** podem começar nem acabar com `/`.
- **Não** podem conter `//` (duas barras consecutivas) em nenhum data element.
- Caracteres especiais fora da lista devem ser representados (boas práticas):
  `€ → E`, `& → +`, `_ → -`. **`[CONFIRMAR]`** (mapa de `@` ficou ilegível no txt).

### 1.2 Estrutura do ficheiro — §3.4 (camada E)
- Um ficheiro só pode conter **um tipo de mensagem** (só `pain.001` **ou** só
  `pain.008`) — nunca misturado.
- **Não** pode haver mensagens em duplicado.
- Recomendado `CRLF` após cada fecho de tag (a não utilização pode inviabilizar o
  tratamento pelo banco). → tratar como **aviso**, não erro bloqueante. `[CONFIRMAR]`

### 1.3 Datas (camada N)
- `CreDtTm` → **ISO DateTime** (`YYYY-MM-DDThh:mm:ss`). O erro do screenshot
  (`202611-27T00:00:00`) falha aqui.
- `ReqdExctnDt` (pain.001) / `ReqdColltnDt` (pain.008) / `DtOfSgntr` → **ISO Date**
  (`YYYY-MM-DD`).
- Limite mínimo de data de processamento (o screenshot mostrou "≥ 2026-06-24"): é uma
  regra **dinâmica** (data-limite/antecedência), não está fixa no manual → parametrizar
  (`VALIDATOR_MIN_PROCESSING_DATE` ou regra d-1/d+n). **`[CONFIRMAR]` com o negócio.**

### 1.4 Montantes — `InstdAmt` / `CtrlSum` (camada N)
- Moeda: **só `EUR`**.
- `InstdAmt`: `0 < montante ≤ 999999999.99`, **máx. 2 casas decimais**.
- `CtrlSum` = somatório dos `InstdAmt` do grupo/mensagem, máx. 2 decimais.
- `NbOfTxs` = contagem real de transações. **`[CONFIRMAR]`** se validamos a coerência
  `CtrlSum`↔soma e `NbOfTxs`↔contagem (o manual define os campos; a reconciliação é
  boa prática de validador).

### 1.5 Identificadores bancários (camada N)
- **IBAN** — máx. 34; validar por **ISO 13616** (check digits); os 2 primeiros
  caracteres = país **ISO 3166 alpha-2**.
- **BIC** — 11 posições; **opcional** em geral, **mandatório apenas** para operações
  SEPA destinadas a **países fora da UE** (a acordar com o banco de apoio).
- Quando não se usa BIC, `FinInstnId/Othr/Id` deve ser **`NOTPROVIDED`**.
- **`Ctry`** — 2 caracteres alfa **maiúsculos**, ISO 3166 (o erro do screenshot `de`
  falha por não estar em maiúsculas → `DE`).

### 1.6 Códigos ISO por tabela (camada N) — bundle dos Anexos
- `CtgyPurp/Cd` → **Anexo 5** (Category Purpose).
- `Purp/Cd` → **Anexo 6** (External Purpose Code ISO 20022).
- `CdtrRefInf/.../Cd` → só **`SCOR`**.
- `SvcLvl/Cd` (pain.008) → só **`SEPA`**; `LclInstrm/Cd` → **`CORE`** ou **`B2B`**;
  `SeqTp` → `FRST`/`OOFF`/`RCUR`/`FNAL`.
- Referência do credor: pode usar **ISO 11649** (RF).
- **Ação Fase 1:** extrair as tabelas dos Anexos 5/6 para listas de códigos. `[CONFIRMAR]`

---

## 2. `pain.001.001.03` — Customer Credit Transfer Initiation (§3.5)

Root: `<Document>/<CstmrCdtTrfInitn>` `[1..1]`. Blocos: **A. GrpHdr** (1x) +
**B. PmtInf** (1..n).

### 2.1 Group Header — §3.5.1 (pág. 25–26)
| Índice | Tag | Card. ISO→C2B | Máx | Formato | Regra | Cam. |
|---|---|---|---|---|---|---|
| 1.0 | `GrpHdr` | [1..1] | | | | S |
| 1.1 | `GrpHdr/MsgId` | [1..1] | 35 | texto | Identificação única da mensagem | S/N |
| 1.2 | `GrpHdr/CreDtTm` | [1..1] | | ISO DateTime | Data/hora de criação | N |
| 1.6 | `GrpHdr/NbOfTxs` | [1..1] | 15 | Max15NumericText | Nº de transações da mensagem | N |
| 1.7 | `GrpHdr/CtrlSum` | [0..1]→**[1..1]** | 18 | Decimal, 2 dec. | Somatório dos `InstdAmt` | N |
| 1.8 | `GrpHdr/InitgPty` | [1..1] | | | Nome **ou** Id tem de estar presente | N |
| 9.1.0 | `InitgPty/Nm` | [0..1] | 140 | texto | Só **70** posições úteis | N |
| 9.1.12 | `InitgPty/Id` | [0..1] | | | Uso condicionado a acordo c/ banco | N |
| 9.1.28 | `InitgPty/Id/PrvtId/Othr/Id` | [1..1] | 35 | texto | Senão, banco assume `NOTPROVIDED` | N |

### 2.2 Payment Information — §3.5.2 (pág. 26–28) `[1..n]`
| Índice | Tag | Card. ISO→C2B | Máx | Formato | Regra | Cam. |
|---|---|---|---|---|---|---|
| 2.1 | `PmtInf/PmtInfId` | [1..1] | 35 | texto | Identificação única do grupo | S/N |
| 2.2 | `PmtInf/PmtMtd` | [1..1] | 3 | texto | Só **`TRF`** | N |
| 2.4 | `PmtInf/NbOfTxs` | [0..1]→**[1..1]** | 15 | numeric | Nº transações do grupo | N |
| 2.5 | `PmtInf/CtrlSum` | [0..1]→**[1..1]** | 18 | Decimal, 2 dec. | Somatório do grupo | N |
| 2.13 | `.../LclInstrm/Prtry` | [1..1] | 35 | texto | **`URG`** nas transferências urgentes | N |
| 2.15 | `.../CtgyPurp/Cd` | [1..1] | 4 | ISO | Código ISO (Anexo 5) | N |
| 2.17 | `PmtInf/ReqdExctnDt` | [1..1] | | ISO Date | Data de lançamento (AT-07) | N |
| 2.19 | `PmtInf/Dbtr` | [1..1] | | | Ordenante | S |
| 9.1.0 | `Dbtr/Nm` | [0..1]→**[1..1]** | 140 | texto | Nome do Ordenante, **70** úteis | N |
| 9.1.10 | `Dbtr/PstlAdr/Ctry` | [0..1] | 2 | ISO 3166 maiúsc. | **Obrigatório se `AdrLine` presente** | N |
| 9.1.11 | `Dbtr/PstlAdr/AdrLine` | [0..7]→**[0..2]** | 70 | texto | Máx. 2 ocorrências | N |
| 1.1.1 | `DbtrAcct/Id/IBAN` | [1..1] | 34 | IBAN | IBAN do Ordenante (AT-01) | N |
| 6.1.1 | `DbtrAgt/FinInstnId/BIC` | [0..1] | 11 | BIC | BIC do Ordenante **ou** `Othr/Id=NOTPROVIDED` | N |
| 2.30 | `CdtTrfTxInf/PmtId/EndToEndId` | [1..1] | 35 | texto | Referência; senão `NOTPROVIDED` (AT-41) | N |
| 2.43 | `CdtTrfTxInf/Amt/InstdAmt` | [1..1] | 18 | Decimal (Ccy) | **EUR**, `0<x≤999999999.99`, 2 dec. | N |
| 2.77 | `CdtTrfTxInf/CdtrAgt/.../BIC` | [0..1] | 11 | BIC | Não usar se BIC vazio; obrigatório fora UE | N |
| 2.79 | `CdtTrfTxInf/Cdtr` | [0..1]→**[1..1]** | | | Destinatário | N |
| 9.1.0 | `Cdtr/Nm` | [0..1]→**[1..1]** | 140 | texto | Nome do Destinatário (AT-21), **70** úteis | N |
| 9.1.10 | `Cdtr/PstlAdr/Ctry` | [0..1] | 2 | ISO 3166 maiúsc. | Obrigatório se `AdrLine` presente | N |
| 2.80 | `CdtTrfTxInf/CdtrAcct` | [0..1]→**[1..1]** | | | | N |
| 1.1.1 | `CdtrAcct/Id/IBAN` | [1..1] | 34 | IBAN | ISO 13616 (AT-20) | N |
| 2.87 | `.../Purp/Cd` | [1..1] | 4 | ISO | External Purpose (Anexo 6) | N |
| 2.98 | `CdtTrfTxInf/RmtInf` | [0..1] | | | `Ustrd` **ou** `Strd` | N |
| 2.99 | `RmtInf/Ustrd` | [0..1] | 140 | texto | 1 ocorrência | N |
| 2.100 | `RmtInf/Strd` | [0..1] | | | 1 ocorrência; bloco ≤140 chars | N |
| 2.123 | `Strd/CdtrRefInf/Tp/CdOrPrtry/Cd` | [1..1] | 4 | ISO | Só **`SCOR`** | N |
| 2.125/2.126 | `CdtrRefInf/Issr` / `.../Ref` | [0..1] | 35* | texto | Se ambos, soma ≤ **46** | N |

## 3. `pain.008.001.02` — Customer Direct Debit Initiation (§3.6)

Root: `<Document>/<CstmrDrctDbtInitn>` `[1..1]`. Blocos: **A. GrpHdr** (1x) +
**B. PmtInf** (1..n) com **DrctDbtTxInf**.

### 3.1 Group Header — §3.6.1 (pág. 30–31)
Igual ao pain.001: `MsgId` [1..1] 35; `CreDtTm` [1..1] ISO DateTime; `NbOfTxs` [1..1] 15;
`CtrlSum` [0..1]→**[1..1]** 18/2dec; `InitgPty` [1..1] (Nm 140/70, ou Id/PrvtId/Othr/Id 35).

### 3.2 Payment Information — §3.6.2 (pág. 31–33) `[1..n]`
| Índice | Tag | Card. ISO→C2B | Máx | Formato | Regra | Cam. |
|---|---|---|---|---|---|---|
| 2.2 | `PmtInf/PmtMtd` | [1..1] | 3 | texto | Só **`DD`** | N |
| 2.4/2.5 | `PmtInf/NbOfTxs` / `CtrlSum` | [0..1]→**[1..1]** | 15 / 18 | | Como pain.001 | N |
| 2.6 | `PmtInf/PmtTpInf` | [0..1]→**[1..1]** | | | Mandatório | N |
| 2.9 | `PmtTpInf/SvcLvl/Cd` | [1..1] | 4 | ISO | Só **`SEPA`** (AT-20) | N |
| 2.12 | `PmtTpInf/LclInstrm/Cd` | [1..1] | 35 | ISO | **`CORE`** ou **`B2B`**; não coexistem na msg | N |
| 2.14 | `PmtTpInf/SeqTp` | [0..1]→**[1..1]** | 4 | ISO | `FRST`/`OOFF`/`RCUR`/`FNAL` | S/N |
| 2.16 | `PmtTpInf/CtgyPurp/Cd` | [1..1] | 4 | ISO | Anexo 5 | N |
| 2.18 | `PmtInf/ReqdColltnDt` | [1..1] | | ISO Date | Data de cobrança (AT-11); **RCUR/FNAL ≥ (ou > se AmdmntInd) data do último DD** | N |
| 9.1.0 | `Cdtr/Nm` | [0..1]→**[1..1]** | 140 | texto | Nome do Credor (AT-03), **70** úteis | N |
| 9.1.10 | `Cdtr/PstlAdr/Ctry` | [0..1] | 2 | ISO 3166 maiúsc. | Obrigatório se `AdrLine` presente | N |
| 1.1.1 | `CdtrAcct/Id/IBAN` | [1..1] | 34 | IBAN | IBAN do Credor (AT-04) | N |
| 6.1.1 | `CdtrAgt/FinInstnId/BIC` | [0..1] | 11 | BIC | BIC do Credor **ou** `Othr/Id=NOTPROVIDED` | N |
| 2.27 | `PmtInf/CdtrSchmeId` | [0..1]→**[1..1]** | | | Identificação do Credor (mandatório) | N |
| 9.1.28 | `CdtrSchmeId/.../Othr/Id` | [1..1] | 35 | texto | Creditor Identifier — regra AT-02 | N |

### 3.3 Direct Debit Transaction Information — `DrctDbtTxInf` `[1..n]` (pág. 33–36)
| Índice | Tag | Card. ISO→C2B | Máx | Formato | Regra | Cam. |
|---|---|---|---|---|---|---|
| 2.31 | `.../PmtId/EndToEndId` | [1..1] | 35 | texto | Referência (AT-10); senão `NOTPROVIDED` (pode impedir Reversão) | N |
| 2.44 | `.../InstdAmt` | [1..1] | 18 | Decimal (Ccy) | **EUR**, `0<x≤999999999.99`, 2 dec. | N |
| 2.46 | `.../DrctDbtTx` | [0..1]→**[1..1]** | | | Mandatório | N |
| 2.47 | `.../MndtRltdInf` | [0..1]→**[1..1]** | | | Mandatório | N |
| 2.48 | `MndtRltdInf/MndtId` | [0..1]→**[1..1]** | 35 | texto | Id da autorização (AT-01); **sem espaços no início** | N |
| 2.49 | `MndtRltdInf/DtOfSgntr` | [0..1]→**[1..1]** | | ISO Date | Data de assinatura (AT-25) | N |
| 2.50 | `MndtRltdInf/AmdmntInd` | [0..1] | | boolean | `true`→exige ≥1 de `AmdmntInfDtls` (2.51–2.58); `false`/ausente→nenhuma; **OOFF nunca `true`** | N |
| 2.52 | `.../OrgnlMndtId` | [0..1] | 35 | texto | Obrigatório se muda o Id do mandato | N |
| 2.53 | `.../OrgnlCdtrSchmeId` | [0..1] | | | Obrigatório se muda Creditor Scheme Id ou Nome | N |
| 1.1.3 | `.../OrgnlDbtrAcct/.../Othr/Id` | [1..1] | 34 | texto | **`SMNDA`** se muda conta do Devedor | N |
| 2.58 | `.../OrgnlDbtrAgt/.../Othr/Id` | [0..1] | 35 | texto | `SMNDA` se muda banco; exclui `OrgnlDbtrAcct` | N |
| 2.70 | `.../DbtrAgt/FinInstnId/BIC` | [0..1] | 11 | BIC | BIC do Devedor **ou** `Othr/Id=NOTPROVIDED`; obrigatório fora UE | N |
| 2.72 | `.../Dbtr/Nm` | [0..1]→**[1..1]** | 140 | texto | Nome do Devedor (AT-14), **70** úteis | N |
| 2.73 | `.../DbtrAcct/Id/IBAN` | [1..1] | 34 | IBAN | ISO 13616; 2 primeiros = país ISO 3166 (AT-07) | N |
| 2.74 | `.../UltmtDbtr/Nm` | [0..1] | 140 | texto | Obrigatório se constar no Mandato (AT-15), **70** úteis | N |
| 2.77 | `.../Purp/Cd` | [1..1] | 4 | ISO | Anexo 6 | N |
| 2.88 | `.../RmtInf` | [0..1] | | | `Ustrd` **XOR** `Strd` (não coexistem) | N |
| 2.113 | `.../CdtrRefInf/.../Cd` | [1..1] | 4 | ISO | Só **`SCOR`**; `Issr`+`Ref` soma ≤46 | N |

---

## 4. Distilação para a Camada 3 (regras de conteúdo a implementar)
Regras determinísticas, independentes do XSD, aplicáveis por `layout`:

1. **charset** (todos os data elements de texto): alfabeto §1.1 + regras `/`.
2. **datas**: `CreDtTm` ISO DateTime; `ReqdExctnDt`/`ReqdColltnDt`/`DtOfSgntr` ISO Date;
   limite mínimo de processamento (parametrizável) `[CONFIRMAR]`.
3. **montante/moeda**: `InstdAmt` EUR, `0<x≤999999999.99`, ≤2 dec.; `CtrlSum` 2 dec.;
   (opcional) reconciliação `CtrlSum`/`NbOfTxs`.
4. **IBAN** ISO 13616 + país ISO 3166; **BIC** 11 + regra fora-UE + `NOTPROVIDED`.
5. **`Ctry`** maiúsculas ISO 3166; obrigatório se há `AdrLine`.
6. **códigos fixos**: `PmtMtd` (`TRF`/`DD`), `SvcLvl=SEPA`, `LclInstrm∈{CORE,B2B}`,
   `SeqTp∈{FRST,OOFF,RCUR,FNAL}`, `CdtrRefInf/Cd=SCOR`; `CtgyPurp`/`Purp` ⊂ Anexos 5/6.
7. **tamanhos**: `MsgId/PmtInfId/EndToEndId/…=35`, `Nm=140` (70 úteis), `AdrLine=70`
   (máx 2), `Ustrd/Strd bloco ≤140`, `Issr+Ref ≤46`.
8. **overrides de obrigatoriedade C2B** (as colunas `[0..1]→[1..1]`) — tratar como
   mandatórios embora o XSD os aceite ausentes.
9. **regras de mandato (pain.008)**: `AmdmntInd`/`AmdmntInfDtls`, `SMNDA`, OOFF≠true.
10. **estrutura**: um só tipo de mensagem por ficheiro; sem duplicados.

## 5. Itens `[CONFIRMAR]` com o negócio antes de produção
- Limite mínimo / antecedência da **data de processamento** (regra dinâmica; não no manual).
- **NIF/NIPC** e outras regras próprias do banco (ainda não fornecidas).
- Mapa completo de **caracteres especiais** (parte ilegível no txt).
- Uso do **CRLF** como erro vs aviso.
- Reconciliação `CtrlSum`↔soma e `NbOfTxs`↔contagem (validar sim/não).
- Tabelas de códigos dos **Anexos 5/6** (a extrair na Fase 1).
- **XSD aprimorado interno** (quando chegar) substitui/estende os XSD ISO do bundle.

---
*Gerado na Fase 0. Não inclui pain.007/pain.002 (fora do âmbito PoC). Origem:
`assets/manual/` (manual BdP) — ver também `assets/schemas/` (XSD ISO).*
