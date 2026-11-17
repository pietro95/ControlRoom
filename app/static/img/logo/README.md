# Logo Siciliacque

File dell'identità visiva aziendale (ottobre 2026), esportati da Adobe Illustrator. Uso riservato
all'applicazione interna.

| File | Uso |
|---|---|
| `siciliacque.svg` | logo a colori, su fondo chiaro |
| `siciliacque_bianco.svg` | su fondo scuro (blu istituzionale) |
| `siciliacque_nero.svg` | stampa in bianco e nero |
| `siciliacque_bianco_38@1x/@2x.png` | intestazione del portale (175×38 e 349×76) |
| `siciliacque_bianco_54@1x/@2x.png`, `siciliacque_54@1x/@2x.png` | login, tema scuro e chiaro (≈248×54 e ≈496×108) |
| `siciliacque_pdf.png` | PDF dei report: `siciliacque.svg` rasterizzato (921×216 px) con Chromium; l'SVG, da 339 KB, ripetuto su ogni pagina rende i PDF pesanti |

Unica modifica rispetto agli originali: il `viewBox`, ristretto al disegno per eliminare i
margini vuoti dell'esportazione. `viewBox` ristretti all'ingombro visibile misurato sui pixel (colori `23 188 909 204`, bianco/nero `1 33 562 128`):
entrambe le versioni hanno così le stesse proporzioni (≈4,4:1) e a parità di larghezza la stessa dimensione.
Linee guida tipografiche: `docs/design/linee_guida_tipografia.pdf`. Copiati dal repo `gestione-contratti`.

## PNG a dimensione fissa per il portale
A 38–54 px di altezza l'albero (tratti molto sotto il pixel) si impasta se il browser ridimensiona l'SVG o un PNG
grande. I PNG del portale sono quindi già alla misura esatta di visualizzazione, in versione 1x e 2x (`srcset`), così
il browser non ricampiona. Procedura: SVG renderizzato con Chromium a 3372 px di larghezza su fondo trasparente,
ritaglio sull'ingombro, riduzione con filtro Lanczos (Pillow), come un ridimensionamento di qualità in GIMP.
Per cambiare dimensione rigenerare i PNG alla nuova misura: non scalarli via CSS.
